from fastapi import HTTPException
from sqlalchemy import func, select

from src.models.database import engine, fetch_one
from src.models.tables import (audit, datasets, deliveries, remediation_claims, remediation_dispatches,
                               remediation_plans, runs, schedules)


def list_rows(table, fields=None, limit=100):
    statement = select(*fields) if fields else select(table)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement.order_by(table.c.id.desc()).limit(limit)).mappings()]


def revisions():
    columns = [field for field in datasets.c if field.name != 'records']
    return list_rows(datasets, columns)


def require_row(table, key, label):
    with engine.connect() as connection:
        row = fetch_one(connection, table, key)
    if row is None:
        raise HTTPException(404, label + '不存在。')
    return dict(row)


def jobs():
    return list_rows(runs, [field for field in runs.c if field.name != 'result'])


def delivery_status():
    return {'schedules': list_rows(schedules), 'deliveries': list_rows(deliveries)}


def recent_activity():
    return list_rows(audit, limit=20)


def remediation_list(limit=50):
    with engine.connect() as connection:
        plans = connection.execute(select(remediation_plans).order_by(remediation_plans.c.id.desc())
                                   .limit(limit)).mappings().all()
        claimed = dict(connection.execute(select(remediation_claims.c.plan_id, func.count())
                                          .group_by(remediation_claims.c.plan_id)).all())
        published = dict(connection.execute(select(remediation_dispatches.c.plan_id, func.count())
                                            .group_by(remediation_dispatches.c.plan_id)).all())
    return [{'id': row['id'], 'dataset_id': row['dataset_id'], 'shift_date': row['shift_date'],
             'revision': row['revision'], 'state': row['state'], 'owner': row['owner'],
             'created': row['created'], 'published_at': row['published'],
             'headline': (row['result'] or {}).get('headline'),
             'claimed': claimed.get(row['id'], 0), 'published': published.get(row['id'], 0)}
            for row in plans]
