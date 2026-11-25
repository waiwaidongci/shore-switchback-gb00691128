"""Supplement reporting, versioning and approved-report pinning."""

import csv
import io
import time

from sqlalchemy import select

from src.models.tables import base_versions, runs
from src.services.data import PROFILE, convert
from src.services.domain import compute, sample_rows
from src.services.revisions import activate_run

COLUMNS = [field['key'] for field in PROFILE['columns']]


def csv_text(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return stream.getvalue()


def token(client, user):
    response = client.post('/login', json={'username': user, 'password': f'{user}-demo-24'})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def import_base(client, analyst, rows, name='base.csv'):
    response = client.post('/datasets', json={'name': name, 'csv': csv_text(rows)},
                           headers=analyst)
    assert response.status_code == 201, response.text
    return response.json()['id']


def run_analysis(client, analyst, dataset_id):
    run_id = client.post('/runs', json={'dataset_id': dataset_id, 'options': {}},
                         headers=analyst).json()['id']
    from src.services.workflow import claim_run, finish_run
    key, claim, loaded, options = claim_run()
    finish_run(key, claim, compute(loaded, options), None)
    return run_id


def supplement(client, analyst, base_id, rows, name='supp.csv'):
    return client.post('/datasets/supplement',
                       json={'base_id': base_id, 'name': name, 'csv': csv_text(rows)},
                       headers=analyst)


def test_merge_order_independent_and_idempotent(client):
    analyst = token(client, 'analyst')
    rows = sample_rows()
    missing = [rows[0], rows[1], rows[36], rows[37]]
    keep = [row for row in rows if row not in missing]
    base_id = import_base(client, analyst, keep)
    first = supplement(client, analyst, base_id, [missing[0], missing[1]], 'a.csv').json()
    second = supplement(client, analyst, base_id, [missing[2], missing[3]], 'b.csv').json()
    assert first['changed'] and first['report']['filled_windows'] == 2
    assert second['changed'] and second['report']['remaining_missing_windows'] == 0
    assert second['version'] == 2

    replay = supplement(client, analyst, base_id, [missing[0], missing[1]], 'a-again.csv').json()
    assert replay['changed'] is False
    assert replay['version'] == 2
    assert replay['report']['filled_windows'] == 4


def test_same_window_same_counts_counts_once_and_conflicts_stay_missing(client):
    analyst = token(client, 'analyst')
    rows = sample_rows()
    missing = [rows[0], rows[1]]
    keep = [row for row in rows if row not in missing]
    base_id = import_base(client, analyst, keep)

    supplement(client, analyst, base_id, [missing[0]], 'a.csv')
    same = supplement(client, analyst, base_id, [missing[0]], 'b.csv').json()
    assert same['report']['duplicate_supplements'] >= 1
    assert same['report']['filled_windows'] == 1

    rival = dict(missing[1])
    rival['eligible'] = rival['eligible'] + 5
    supplement(client, analyst, base_id, [missing[1]], 'c1.csv')
    conflict = supplement(client, analyst, base_id, [rival], 'c2.csv').json()
    assert conflict['report']['conflict_windows'] == 1
    assert conflict['report']['remaining_missing_windows'] == 1
    assert len(conflict['report']['conflicts'][0]['counts']) == 2


def test_supplement_cannot_touch_reported_window(client):
    analyst = token(client, 'analyst')
    rows = sample_rows()
    missing = [rows[0]]
    keep = [row for row in rows if row not in missing]
    base_id = import_base(client, analyst, keep)
    response = supplement(client, analyst, base_id, [rows[5]], 'bad.csv')
    assert response.status_code == 422
    assert '已有原始记录' in response.json()['detail']


def test_recompute_uses_merged_windows(client):
    analyst, viewer = token(client, 'analyst'), token(client, 'viewer')
    rows = sample_rows()
    missing = rows[:2]
    keep = [row for row in rows if row not in missing]
    base_id = import_base(client, analyst, keep)
    result = supplement(client, analyst, base_id, missing, 'fix.csv').json()
    assert result['changed'] and result['report']['remaining_missing_windows'] == 0
    run = client.get(f"/runs/{result['run_id']}", headers=viewer).json()
    assert run['is_current'] is True
    assert run['result']['metrics']['窗口覆盖率%'] == 100.0
    assert run['result']['metrics']['B−A配对提升（百分点）'] == 16.03
    assert run['result']['metrics']['单侧随机化p值'] == 0.0025


def test_late_old_run_cannot_override_newer_version(client):
    analyst = token(client, 'analyst')
    rows = sample_rows()
    missing = rows[:2]
    keep = [row for row in rows if row not in missing]
    base_id = import_base(client, analyst, keep)
    result = supplement(client, analyst, base_id, missing, 'fix.csv').json()
    new_run = result['run_id']
    import src.models.database as database
    with database.engine.begin() as connection:
        old_run = connection.execute(runs.insert().values(
            dataset_id=base_id, options={}, state='completed', owner='x', created=0,
            started=0, finished=time.time(),
            result={'headline': 'OLD', 'can_publish': True, 'rows': [], 'metrics': {},
                    'notes': [], 'chart': {'points': []}})).inserted_primary_key[0]
        assert activate_run(connection, old_run, time.time()) is False
    with database.engine.connect() as connection:
        pointer = connection.execute(select(base_versions).where(
            base_versions.c.base_id == base_id)).mappings().one()
    assert pointer['active_run_id'] == new_run


def test_approved_report_is_pinned_across_merge(client):
    analyst, reviewer, viewer = (token(client, 'analyst'), token(client, 'reviewer'),
                                 token(client, 'viewer'))
    rows = sample_rows()
    washout = [row for row in rows if row['pair'] == 1 and row['block'] == 1
               and row['window_start'].endswith('08:00')]
    keep = [row for row in rows if row not in washout]
    base_id = import_base(client, analyst, keep)
    run_id = run_analysis(client, analyst, base_id)
    decision = client.post(f'/runs/{run_id}/review',
                           json={'decision': 'approved', 'note': '批准初版'}, headers=reviewer)
    assert decision.status_code == 200, decision.text
    before = client.get(f'/runs/{run_id}/report.csv', headers=viewer)
    assert before.status_code == 200

    result = supplement(client, analyst, base_id, washout, 'wash.csv').json()
    assert result['changed']
    after = client.get(f'/runs/{run_id}/report.csv', headers=viewer)
    assert after.status_code == 200
    assert after.content == before.content
    detail = client.get(f'/runs/{run_id}', headers=viewer).json()
    assert detail['approved_version'] == 0
    assert detail['superseded'] is True


def test_supplement_of_supplement_base_is_rejected(client):
    analyst = token(client, 'analyst')
    rows = sample_rows()
    missing = rows[:2]
    keep = [row for row in rows if row not in missing]
    base_id = import_base(client, analyst, keep)
    result = supplement(client, analyst, base_id, missing, 'fix.csv').json()
    repeated = supplement(client, analyst, result['supplement_id'], missing, 'again.csv')
    assert repeated.status_code == 422
    assert '补报基底' in repeated.json()['detail']


def test_concurrent_arrivals_converge_to_same_windows(client):
    import threading

    import src.models.database as database
    from src.services.revisions import import_supplement
    from sqlalchemy import select as sa_select

    from src.models.tables import base_versions, datasets
    from src.services.data import checksum_for, insert_revision

    analyst = token(client, 'analyst')
    rows = sample_rows()
    missing = [rows[0], rows[1], rows[2], rows[36], rows[37], rows[38]]
    keep = [row for row in rows if row not in missing]
    files = [[row] for row in missing]

    reference = None
    for trial in range(4):
        with database.engine.begin() as connection:
            base_id, _ = insert_revision(
                connection, f'base{trial}', records_of(keep), 'analyst',
                checksum_for(records_of(keep), f'trial-{trial}'), time.time())
        from src.services.revisions import register_base
        register_base(base_id)
        errors = []

        def submit(name, rows_to_send):
            try:
                import_supplement(base_id, name, records_of(rows_to_send), 'analyst')
            except Exception as error:  # pragma: no cover - surfaced below
                errors.append(error)

        threads = [threading.Thread(target=submit, args=(f'{index}.csv', file_rows))
                   for index, file_rows in enumerate(files)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        with database.engine.connect() as connection:
            pointer = connection.execute(sa_select(base_versions).where(
                base_versions.c.base_id == base_id)).mappings().one()
            merged = connection.execute(sa_select(datasets.c.records).where(
                datasets.c.id == pointer['active_dataset_id'])).scalar()
        assert pointer['active_seq'] == len(files)
        signature = sorted(str(row) for row in merged)
        if reference is None:
            reference = signature
        assert signature == reference


def records_of(rows):
    return [{field['key']: convert(row[field['key']], field) for field in PROFILE['columns']}
            for row in rows]
