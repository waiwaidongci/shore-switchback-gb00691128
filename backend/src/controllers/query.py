from fastapi import HTTPException
from sqlalchemy import select

from src.models.database import engine, fetch_one
from src.models.tables import audit, datasets, deliveries, runs, schedules, supplement_links
from src.services.revisions import annotate, latest_merge_report, revision_info


def list_rows(table, fields=None, limit=100):
    statement = select(*fields) if fields else select(table)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement.order_by(table.c.id.desc()).limit(limit)).mappings()]


def revisions():
    columns = [field for field in datasets.c if field.name != 'records']
    rows = list_rows(datasets, columns, limit=500)
    with engine.connect() as connection:
        return annotate(connection, rows, kind='dataset')


def require_row(table, key, label):
    with engine.connect() as connection:
        row = fetch_one(connection, table, key)
    if row is None:
        raise HTTPException(404, label + '不存在。')
    return dict(row)


def jobs():
    rows = list_rows(runs, [field for field in runs.c if field.name != 'result'], limit=500)
    with engine.connect() as connection:
        return annotate(connection, rows)


def dataset_detail(key):
    with engine.connect() as connection:
        row = fetch_one(connection, datasets, key)
        if row is None:
            raise HTTPException(404, '数据版本不存在。')
        info = revision_info(connection, key)
        records = row['records']
        report = latest_merge_report(connection, info['base_id']) if info else None
        supplements = connection.execute(select(supplement_links.c.supplement_id)
                                         .where(supplement_links.c.base_id == info['base_id'])
                                         .order_by(supplement_links.c.id)).scalars().all() if info else []
    detail = {field: row[field] for field in row if field != 'records'}
    return {**detail, 'count': len(records), 'records': records[:100],
            'kind': info['kind'] if info else 'base', 'base_id': info['base_id'] if info else key,
            'version': info['seq'] if info else 0, 'supplements': list(supplements),
            'merge_report': report}


def delivery_status():
    return {'schedules': list_rows(schedules), 'deliveries': list_rows(deliveries)}


def recent_activity():
    return list_rows(audit, limit=20)
