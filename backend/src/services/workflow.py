import time

from fastapi import HTTPException
from sqlalchemy import and_, insert, or_, select, update

from src.constants.enums import State
from src.config.settings import ARTIFACTS
from src.models.database import add_event, engine, fetch_one
from src.models.tables import datasets, deliveries, runs, schedules
from src.services.data import parse_options
from src.services.domain import compute
from src.services.revisions import (activate_run, annotate, approval_pin, pin_approval,
                                    revision_info)
from src.utils.exports import write_csv


def run_detail(key):
    """Single run with lineage flags (current / superseded / pending)."""
    with engine.connect() as connection:
        row = fetch_one(connection, runs, key)
        if row is None:
            raise HTTPException(404, '分析任务不存在。')
        detail = annotate(connection, [dict(row)], kind='run')[0]
        pin = approval_pin(connection, key)
    if pin:
        detail['approved_dataset_id'] = pin['dataset_id']
        detail['approved_version'] = pin['version']
    return detail


def add_run(body, actor):
    options = parse_options(body.options)
    now = time.time()
    with engine.begin() as connection:
        if fetch_one(connection, datasets, body.dataset_id) is None:
            raise HTTPException(404, '数据版本不存在。')
        info = revision_info(connection, body.dataset_id)
        if info is not None and info['kind'] == 'supplement':
            raise HTTPException(422, '补报文件不能直接分析，请分析其合并后的数据版本。')
        key = connection.execute(insert(runs).values(dataset_id=body.dataset_id, options=options,
                    state=State.queued.value, owner=actor, created=now)).inserted_primary_key[0]
        add_event(connection, actor, 'queue', key, now)
    return {'id': key, 'state': State.queued.value}


def claim_run():
    now = time.time()
    claimable = or_(runs.c.state == State.queued.value, and_(runs.c.state == State.running.value, runs.c.started < now - 120))
    with engine.begin() as connection:
        run = connection.execute(select(runs).where(claimable).order_by(runs.c.id).limit(1)).mappings().one_or_none()
        if run is None:
            return None
        key = run['id']
        claim = connection.execute(update(runs).where(runs.c.id == key, claimable)
                                   .values(state=State.running.value, started=now))
        if claim.rowcount != 1:
            return None
        data = fetch_one(connection, datasets, run['dataset_id'])
        return key, now, data['records'], run['options']


def finish_run(key, claim, result, error):
    now = time.time()
    state = State.failed.value if error else State.completed.value
    with engine.begin() as connection:
        saved = connection.execute(update(runs).where(runs.c.id == key, runs.c.started == claim,
                                   runs.c.state == State.running.value).values(state=state, result=result,
                                                  error=error, finished=now))
        if saved.rowcount:
            add_event(connection, 'worker', state, key, now)
            if not error:
                activate_run(connection, key, now)


def report_data(key):
    """Return report bytes and the dataset version they belong to.

    An approved run is pinned to the version and windows reviewed at approval time,
    so later supplement merges neither rewrite it nor block it. A draft run
    recomputes on its own recorded version.
    """
    with engine.connect() as connection:
        run = fetch_one(connection, runs, key)
        if run is None:
            raise HTTPException(404, '分析任务不存在。')
        pin = approval_pin(connection, key)
        info = revision_info(connection, run['dataset_id'])
    if not run['result']:
        raise HTTPException(409, '分析结果尚不可导出。')
    version = pin['version'] if pin else (info['seq'] if info else 0)
    dataset_id = pin['dataset_id'] if pin else run['dataset_id']
    path = ARTIFACTS / f"run-{key}-{run['started'].hex()}.csv"
    if path.exists():
        return path.read_bytes(), version
    dataset = require_dataset(dataset_id)
    result = compute(dataset['records'], run['options'])
    return write_csv(result['rows'], result.get('columns')), version


def require_dataset(dataset_id):
    with engine.connect() as connection:
        row = fetch_one(connection, datasets, dataset_id)
    if row is None:
        raise HTTPException(409, '报告所绑定的数据版本已不可用。')
    return dict(row)


def review_run(key, body, actor):
    now = time.time()
    note = body.note.strip()
    if len(note) < 2:
        raise ValueError('请输入至少两个字符的复核意见。')
    with engine.begin() as connection:
        run = fetch_one(connection, runs, key)
        if run is None:
            raise HTTPException(404, '分析任务不存在。')
        if run['state'] != State.completed.value:
            raise HTTPException(409, '分析完成后才能复核。')
        detail = annotate(connection, [dict(run)], kind='run')[0]
        if body.decision == 'approved':
            if detail.get('superseded'):
                raise HTTPException(409, '该结果基于已被新版取代的数据版本，请复核当前生效版本。')
            if detail.get('pending'):
                raise HTTPException(409, '该结果基于尚未生效的新数据版本，暂不可批准。')
            if run['result'].get('can_publish') is False:
                raise HTTPException(409, '结果未达到本项目的发布门槛。')
        updated = connection.execute(update(runs).where(runs.c.id == key, runs.c.decision.is_(None))
                                     .values(decision=body.decision, reviewer=actor, review_note=note, reviewed=now))
        if updated.rowcount != 1:
            raise HTTPException(409, '已完成复核。请重新运行分析以提交新的复核。')
        if body.decision == 'approved':
            info = revision_info(connection, run['dataset_id'])
            pin_approval(connection, key, run['dataset_id'], info['seq'] if info else 0, now)
        add_event(connection, actor, body.decision, key, now)
    return {'decision': body.decision}


def schedule_report(key, body, actor):
    now = time.time()
    recipient = body.recipient.strip()
    if len(recipient) < 2:
        raise ValueError('请填写站内收件组。')
    if body.scheduled_at and body.scheduled_at.utcoffset() is None:
        raise ValueError('投递时间须含时区。')
    timestamp = body.scheduled_at.timestamp() if body.scheduled_at else now
    if body.scheduled_at and timestamp < now - 60:
        raise ValueError('投递时间已过，请调整时间。')
    with engine.begin() as connection:
        run = fetch_one(connection, runs, key)
        if run is None or run['decision'] != 'approved':
            raise HTTPException(409, '仅已批准的结果可分发。')
        required_recipient = run['result'].get('dispatch_recipient')
        if required_recipient and recipient != required_recipient:
            raise HTTPException(422, f'该报告应投递给{required_recipient}。')
        schedule_id = connection.execute(insert(schedules).values(run_id=key, recipient=recipient, cadence=body.cadence,
                                      next_at=timestamp, active=1, owner=actor)).inserted_primary_key[0]
        add_event(connection, actor, 'schedule', schedule_id, now)
    return {'id': schedule_id, 'next_at': timestamp}


def cancel_schedule(key, actor):
    with engine.begin() as connection:
        updated = connection.execute(update(schedules).where(schedules.c.id == key, schedules.c.active == 1)
                                     .values(active=0))
        if not updated.rowcount:
            raise HTTPException(409, '该计划已停止或已完成。')
        add_event(connection, actor, 'cancel', key, time.time())
    return {'id': key, 'active': False}


def publish_due():
    now = time.time()
    with engine.begin() as connection:
        pending = connection.execute(select(schedules).where(schedules.c.active == 1, schedules.c.next_at <= now)
                                     .order_by(schedules.c.next_at, schedules.c.id).limit(20)).mappings().all()
        for item in pending:
            previous = item['next_at']
            daily = item['cadence'] == 'daily'
            future = previous + (int((now - previous) / 86400) + 1) * 86400 if daily else previous
            changed = connection.execute(update(schedules).where(schedules.c.id == item['id'], schedules.c.active == 1,
                              schedules.c.next_at == previous).values(active=int(daily), next_at=future))
            if changed.rowcount:
                delivery_id = connection.execute(insert(deliveries).values(schedule_id=item['id'], run_id=item['run_id'],
                                               recipient=item['recipient'], scheduled=previous, delivered=now)).inserted_primary_key[0]
                add_event(connection, 'worker', 'delivered', delivery_id, now)
