"""Supplement reporting on top of frozen data revisions.

A *base* revision is the originally imported CSV. Supplemental files may only
fill windows that the base revision never reported. All supplements for a base
are stored as one set and re-merged on every arrival, so the merged window set —
and therefore which windows count as valid — never depends on arrival order.

Merging rules:

* a planned window still missing from the base may be filled;
* several supplements reporting the same window with the same counts count once;
* differing counts for the same window are a conflict and the window stays missing.

Each merge that changes the window set creates a new merged dataset version and
immediately recomputes coverage and the per-zone fixed-quota randomisation on the
merged windows. The new window version and its conclusion are stored and activated
together in one transaction, so a page never shows new windows with an old
conclusion. A failed merge or recomputation rolls back and leaves the previous
active version in force, and a late-finishing run for an older version can never
override a newer one.
"""

import json
import time

from sqlalchemy import and_, insert, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from src.constants.enums import State
from src.models.database import add_event, engine, fetch_one
from src.models.tables import (approval_pins, base_versions, datasets, merge_reports,
                               revision_meta, runs, supplement_links)
from src.services.data import checksum_for, insert_revision
from src.services.domain import compute, merge_supplements


def _meta_map(connection):
    rows = connection.execute(select(revision_meta)).mappings().all()
    return {row['dataset_id']: dict(row) for row in rows}


def revision_info(connection, dataset_id, meta=None):
    """Return kind/base/version for a dataset, treating unregistered rows as bases."""
    meta = meta if meta is not None else _meta_map(connection)
    found = meta.get(dataset_id)
    if found is not None:
        return found
    if fetch_one(connection, datasets, dataset_id) is None:
        return None
    return {'dataset_id': dataset_id, 'kind': 'base', 'base_id': dataset_id, 'seq': 0}


def _pointers(connection):
    return {row['base_id']: dict(row) for row in
            connection.execute(select(base_versions)).mappings().all()}


def annotate(connection, rows, kind='run'):
    """Attach lineage, version and active/superseded flags to run or dataset rows.

    A run flagged ``is_current`` is the one whose windows and conclusion the
    analysis pages should show for its base. Superseded runs remain readable for
    approved reports but never replace a newer result.
    """
    meta = _meta_map(connection)
    pointers = _pointers(connection)
    output = []
    for row in rows:
        item = dict(row)
        dataset_id = item.get('dataset_id') if kind == 'run' else item.get('id')
        info = revision_info(connection, dataset_id, meta)
        if info is not None:
            pointer = pointers.get(info['base_id']) or {}
            active_seq = pointer.get('active_seq', 0)
            active_dataset = pointer.get('active_dataset_id')
            active_run = pointer.get('active_run_id')
            is_version = info['kind'] != 'supplement'
            item['base_id'] = info['base_id']
            item['version'] = info['seq']
            item['kind'] = info['kind']
            item['active_version'] = active_seq
            item['is_active_version'] = is_version and info['dataset_id'] == active_dataset
            item['superseded'] = is_version and bool(pointer) and info['seq'] < active_seq
            item['pending'] = is_version and bool(pointer) and info['seq'] > active_seq
            if kind == 'run':
                item['is_current'] = bool(active_run and active_run == item['id'])
        output.append(item)
    return output


def _safe_insert(connection, table, values):
    try:
        with connection.begin_nested():
            connection.execute(insert(table).values(**values))
        return True
    except IntegrityError:
        return False


def _register_base(connection, dataset_id, now):
    if connection.execute(select(revision_meta.c.dataset_id).where(
            revision_meta.c.dataset_id == dataset_id)).scalar() is None:
        _safe_insert(connection, revision_meta, {'dataset_id': dataset_id, 'kind': 'base',
                                                 'base_id': dataset_id, 'seq': 0, 'created': now})
    if connection.execute(select(base_versions.c.base_id).where(
            base_versions.c.base_id == dataset_id)).scalar() is None:
        _safe_insert(connection, base_versions, {
            'base_id': dataset_id, 'latest_dataset_id': dataset_id, 'latest_seq': 0,
            'active_dataset_id': None, 'active_seq': 0, 'active_run_id': None, 'updated': now})


def register_base(dataset_id, now=None):
    """Record a freshly imported base revision and its initial pointers."""
    now = now or time.time()
    with engine.begin() as connection:
        _register_base(connection, dataset_id, now)


def bootstrap(now=None):
    """Backfill lineage for datasets imported before supplement support.

    Every dataset without lineage metadata is treated as its own base. If a base
    has no active run yet, the newest completed run on its base version becomes
    current, so existing results stay visible after an upgrade.
    """
    now = now or time.time()
    with engine.begin() as connection:
        known = set(connection.execute(select(revision_meta.c.dataset_id)).scalars().all())
        existing = connection.execute(select(datasets.c.id, datasets.c.created)
                                      .order_by(datasets.c.id)).mappings().all()
        for row in existing:
            if row['id'] in known:
                continue
            _register_base(connection, row['id'], row['created'] or now)
        pointers = _pointers(connection)
        for base_id, pointer in pointers.items():
            if pointer.get('active_run_id') is not None:
                continue
            latest = connection.execute(select(runs.c.id).where(
                runs.c.dataset_id == base_id, runs.c.state == State.completed.value)
                .order_by(runs.c.id.desc()).limit(1)).scalar()
            if latest is not None:
                connection.execute(update(base_versions).where(base_versions.c.base_id == base_id)
                                   .values(active_dataset_id=base_id, active_seq=0,
                                           active_run_id=latest, updated=now))


def _pointer(connection, base_id, lock=False):
    statement = select(base_versions).where(base_versions.c.base_id == base_id)
    if lock:
        statement = statement.with_for_update()
    return connection.execute(statement).mappings().one()


class _Stale(Exception):
    """Another writer advanced the base pointer first; retry against fresh state."""


def _claim_base(connection, base_id, now):
    """Take the per-base write lock before any dependent read, then return fresh state.

    The first statement is a no-op update: MySQL locks the base row and SQLite takes
    the database write lock. The follow-up pointer read is a locking read, so it sees
    the latest committed state even under MySQL's REPEATABLE READ. Concurrent
    supplement arrivals for the same base thus run one at a time, and each re-reads
    the full supplemental set and latest pointer, so the merged windows never depend
    on arrival order.
    """
    locked = connection.execute(
        update(base_versions).where(base_versions.c.base_id == base_id)
        .values(updated=now)).rowcount
    if not locked:
        _register_base(connection, base_id, now)
        connection.execute(update(base_versions).where(base_versions.c.base_id == base_id)
                           .values(updated=now))
    return _pointer(connection, base_id, lock=True)


def _signature(records):
    return sorted(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
                  for row in records)


def _same_records(left, right):
    return _signature(left) == _signature(right)


def _linked_supplements(connection, base_id):
    ids = connection.execute(select(supplement_links.c.supplement_id)
                             .where(supplement_links.c.base_id == base_id)
                             .with_for_update()
                             .order_by(supplement_links.c.id)).scalars().all()
    if not ids:
        return []
    rows = connection.execute(select(datasets.c.id, datasets.c.records)
                              .where(datasets.c.id.in_(ids)).with_for_update()
                              ).mappings().all()
    by_id = {row['id']: row['records'] for row in rows}
    return [by_id[key] for key in ids if key in by_id]


def _lineage_ids(connection, base_id):
    ids = connection.execute(select(revision_meta.c.dataset_id)
                             .where(revision_meta.c.base_id == base_id)).scalars().all()
    return set(ids) | {base_id}


def _lineage_runs(connection, base_id):
    ids = _lineage_ids(connection, base_id)
    if not ids:
        return []
    return connection.execute(select(runs.c.id, runs.c.dataset_id, runs.c.options, runs.c.state)
                              .where(runs.c.dataset_id.in_(ids)).with_for_update()
                              .order_by(runs.c.id.desc())).mappings().all()


def _latest_options(connection, base_id):
    """Options of the newest completed run in this lineage, for a faithful recompute."""
    for run in _lineage_runs(connection, base_id):
        if run['state'] == State.completed.value:
            return run['options']
    return {}


def _validate_base(connection, base_hint):
    found = revision_info(connection, base_hint)
    if found is None:
        raise ValueError('数据版本不存在。')
    if found['kind'] == 'supplement':
        raise ValueError('补报文件不能作为补报基底，请选择原始数据版本或已合并版本。')
    base_id = found['base_id']
    base = fetch_one(connection, datasets, base_id)
    if base is None:
        raise ValueError('原始数据版本不存在。')
    return base_id, base


def _record_supplement(connection, base_id, name, rows, actor, now):
    supplement_id, created = insert_revision(
        connection, name, rows, actor, checksum_for(rows, f'supplement:{base_id}'), now)
    if created:
        _safe_insert(connection, revision_meta, {
            'dataset_id': supplement_id, 'kind': 'supplement', 'base_id': base_id,
            'seq': 0, 'created': now})
    linked = connection.execute(select(supplement_links.c.id).where(and_(
        supplement_links.c.base_id == base_id,
        supplement_links.c.supplement_id == supplement_id))).scalar()
    if linked is None:
        try:
            with connection.begin_nested():
                connection.execute(insert(supplement_links).values(
                    base_id=base_id, supplement_id=supplement_id, created=now))
        except IntegrityError:
            pass
    return supplement_id


def _merge_once(base_id, base, name, rows, actor, now, max_attempts=8):
    """Merge, recompute and activate in one lock-first transaction, retrying if stale."""
    base_records = base['records']
    last_error = None
    for attempt in range(max_attempts):
        started = time.time()
        try:
            with engine.begin() as connection:
                pointer = _claim_base(connection, base_id, started)
                supplements = _linked_supplements(connection, base_id)
                merged, report = merge_supplements(base_records, supplements + [rows])
                options = _latest_options(connection, base_id)
                result = compute(merged, options)

                supplement_id = _record_supplement(connection, base_id, name, rows, actor, started)
                add_event(connection, actor, 'supplement', supplement_id, started)

                latest = fetch_one(connection, datasets, pointer['latest_dataset_id'])
                same_as_latest = latest is not None and _same_records(merged, latest['records'])
                version = pointer['latest_seq']
                merged_id = pointer['latest_dataset_id']
                run_id = pointer['active_run_id']
                changed = not same_as_latest
                if changed:
                    version += 1
                    merged_id, created_merge = insert_revision(
                        connection, f"{base['name']}（补报合并 #{version}）", merged, actor,
                        checksum_for(merged, f'merged:{base_id}:{version}'), started)
                    if created_merge:
                        _safe_insert(connection, revision_meta, {
                            'dataset_id': merged_id, 'kind': 'merged', 'base_id': base_id,
                            'seq': version, 'created': started})
                    else:
                        connection.execute(update(revision_meta).where(
                            revision_meta.c.dataset_id == merged_id).values(base_id=base_id, seq=version))
                    run_id = connection.execute(insert(runs).values(
                        dataset_id=merged_id, options=options, state=State.completed.value,
                        owner=actor, created=started, started=started, finished=started,
                        result=result)).inserted_primary_key[0]
                    add_event(connection, 'worker', 'completed', run_id, started)

                # Compare-and-set: commit only if no other writer advanced the pointer.
                advanced = connection.execute(
                    update(base_versions).where(
                        base_versions.c.base_id == base_id,
                        base_versions.c.latest_seq == pointer['latest_seq']).values(
                        latest_dataset_id=merged_id, latest_seq=version,
                        active_dataset_id=merged_id, active_seq=version,
                        active_run_id=run_id, updated=started))
                if advanced.rowcount != 1:
                    raise _Stale()
                if changed:
                    add_event(connection, actor, 'merge', merged_id, started)
                    add_event(connection, 'worker', 'activate', run_id, started)

                connection.execute(insert(merge_reports).values(
                    base_id=base_id, merged_id=merged_id, seq=version, report=report, created=started))
                return {
                    'base_id': base_id,
                    'supplement_id': supplement_id,
                    'dataset_id': merged_id,
                    'version': version,
                    'changed': changed,
                    'run_id': run_id if changed else None,
                    'report': report,
                    'headline': result['headline'] if changed else None,
                }
        except _Stale:
            last_error = None
            if attempt + 1 >= max_attempts:
                break
            time.sleep(0.01 * (attempt + 1))
        except OperationalError as error:
            if 'locked' not in str(error).lower() and 'busy' not in str(error).lower():
                raise
            last_error = error
            if attempt + 1 >= max_attempts:
                break
            time.sleep(0.02 * (attempt + 1))
    raise ValueError('补报并发冲突，请稍后重新提交该补报。') from last_error


def import_supplement(base_hint, name, rows, actor):
    """Validate one supplemental file, re-merge the whole set, recompute, and switch.

    Each attempt runs in a single transaction:

    * the full supplemental set is re-merged with this file, so the merged windows
      never depend on arrival order;
    * coverage and the per-zone fixed-quota randomisation are recomputed on the
      merged windows, using the parameters of the newest completed run;
    * the merged window version and its conclusion are activated together;
    * activation is a compare-and-set on the base pointer, so concurrent arrivals
      retry and re-merge instead of dropping each other's windows;
    * any failure rolls back, leaving the previous active version in force.
    """
    if not name.strip():
        raise ValueError('补报名称不能为空。')
    now = time.time()
    try:
        with engine.connect() as connection:
            base_id, base = _validate_base(connection, base_hint)
        return _merge_once(base_id, base, name, rows, actor, now)
    except ValueError:
        with engine.begin() as connection:
            add_event(connection, actor, 'supplement_failed', base_hint, time.time())
        raise


def activation_decision(version, run_id, pointer):
    """Return the pointer fields to sync after a completed run, or None to leave them.

    Newer data versions always win. Within one version, only a run submitted later
    (higher id) may replace the active run, so a late-finishing older result cannot
    top a newer one.
    """
    if version > pointer['active_seq']:
        return version, run_id
    if version == pointer['active_seq']:
        active_run = pointer['active_run_id']
        if active_run is None or run_id > active_run:
            return version, run_id
    return None


def activate_run(connection, run_id, now):
    """Activate a completed run's version if it is the newest valid one.

    Takes the base write lock before reading the pointer, so a newer version's
    activation cannot be overtaken by a slower, older run.
    """
    run = fetch_one(connection, runs, run_id)
    if run is None or run['state'] != State.completed.value or not run['result']:
        return False
    info = revision_info(connection, run['dataset_id'])
    if info is None:
        return False
    pointer = _claim_base(connection, info['base_id'], now)
    decision = activation_decision(info['seq'], run_id, pointer)
    if decision is None:
        return False
    version, active_run = decision
    connection.execute(update(base_versions).where(base_versions.c.base_id == info['base_id'])
                       .values(active_dataset_id=run['dataset_id'], active_seq=version,
                               active_run_id=active_run, updated=now))
    add_event(connection, 'worker', 'activate', run_id, now)
    return True


def pin_approval(connection, run_id, dataset_id, version, now):
    _safe_insert(connection, approval_pins, {
        'run_id': run_id, 'dataset_id': dataset_id, 'version': version, 'approved': now})


def approval_pin(connection, run_id):
    row = connection.execute(select(approval_pins).where(approval_pins.c.run_id == run_id)
                             ).mappings().one_or_none()
    return dict(row) if row else None


def latest_merge_report(connection, base_id):
    row = connection.execute(select(merge_reports).where(merge_reports.c.base_id == base_id)
                             .order_by(merge_reports.c.id.desc()).limit(1)).mappings().one_or_none()
    return dict(row) if row else None
