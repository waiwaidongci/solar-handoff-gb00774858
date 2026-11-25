import time

from fastapi import HTTPException
from sqlalchemy import and_, delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.constants.enums import State
from src.models.database import add_event, engine, fetch_one
from src.models.tables import (datasets, deliveries, remediation_claims, remediation_dispatches,
                               remediation_plans, runs, schedules)
from src.services.data import PROFILE, parse_options
from src.services.domain import remediation, resolve_day


def add_run(body, actor):
    options = parse_options(body.options)
    now = time.time()
    with engine.begin() as connection:
        if fetch_one(connection, datasets, body.dataset_id) is None:
            raise HTTPException(404, '数据版本不存在。')
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
        if body.decision == 'approved' and run['result'].get('can_publish') is False:
            raise HTTPException(409, '结果未达到本项目的发布门槛。')
        updated = connection.execute(update(runs).where(runs.c.id == key, runs.c.decision.is_(None))
                                     .values(decision=body.decision, reviewer=actor, review_note=note, reviewed=now))
        if updated.rowcount != 1:
            raise HTTPException(409, '已完成复核。请重新运行分析以提交新的复核。')
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



# --- 三区补检排班 ---------------------------------------------------------
# 已接单（claimed）和已发布（published）的安排固定不动：重算时它们先钉住
# 班组时段，其设备也不会被再次派单。其余设备重算以“本班排上台数最多”为目标。

def _remediation_options(values):
    return parse_options(values, PROFILE['remediation_options'])


def _fixed_for(connection, plan_id):
    """Pinned assignments: claimed orders plus published dispatches."""
    fixed = {}
    for table, source in ((remediation_claims, 'claimed'), (remediation_dispatches, 'published')):
        for row in connection.execute(select(table).where(table.c.plan_id == plan_id)
                                      .order_by(table.c.id)).mappings():
            fixed[row['device']] = {'device': row['device'], 'zone': row['zone'], 'crew': row['crew'],
                                    'start_minute': row['start_minute'], 'end_minute': row['end_minute'],
                                    'source': source}
    return list(fixed.values())


def _load_plan(connection, plan_id, lock=False):
    statement = select(remediation_plans).where(remediation_plans.c.id == plan_id)
    if lock:
        statement = statement.with_for_update()
    plan = connection.execute(statement).mappings().one_or_none()
    if plan is None:
        raise HTTPException(404, '补检排班不存在。')
    return plan


def _recompute_locked(connection, plan_id):
    """Recompute in read-committed isolation against the locked plan row.

    Holding the row lock serialises claim / release / recompute / publish for a
    single plan, so a concurrent claim can never be lost by a recompute and a
    publish retry observes the same fixed assignments.
    """
    plan = _load_plan(connection, plan_id, lock=True)
    dataset = fetch_one(connection, datasets, plan['dataset_id'])
    if dataset is None:
        raise HTTPException(409, '排班绑定的数据版本已不可用。')
    result = remediation(dataset['records'], plan['options'], _fixed_for(connection, plan_id))
    return plan, result


def _recompute_read(connection, plan_id):
    """Lock-free recompute for GET views; a concurrent claim just shows next refresh."""
    plan = fetch_one(connection, remediation_plans, plan_id)
    if plan is None:
        raise HTTPException(404, '补检排班不存在。')
    dataset = fetch_one(connection, datasets, plan['dataset_id'])
    if dataset is None:
        raise HTTPException(409, '排班绑定的数据版本已不可用。')
    result = remediation(dataset['records'], plan['options'], _fixed_for(connection, plan_id))
    return plan, result


def create_remediation(dataset_id, values, actor):
    """Create (or refresh) the plan row for one dataset + shift date, then compute it."""
    options = _remediation_options(values)
    now = time.time()
    with engine.begin() as connection:
        dataset = fetch_one(connection, datasets, dataset_id)
        if dataset is None:
            raise HTTPException(404, '数据版本不存在。')
        day = resolve_day(dataset['records'], options)
        row = connection.execute(select(remediation_plans).where(
            remediation_plans.c.dataset_id == dataset_id,
            remediation_plans.c.shift_date == day.isoformat()).with_for_update()).mappings().one_or_none()
        if row is not None:
            plan_id = row['id']
            connection.execute(update(remediation_plans).where(remediation_plans.c.id == plan_id)
                               .values(options=options, owner=actor))
        else:
            savepoint = connection.begin_nested()
            try:
                plan_id = connection.execute(insert(remediation_plans).values(
                    dataset_id=dataset_id, shift_date=day.isoformat(), options=options, revision=0,
                    state=State.queued.value, owner=actor, created=now)).inserted_primary_key[0]
                savepoint.commit()
            except IntegrityError as error:
                savepoint.rollback()
                row = connection.execute(select(remediation_plans).where(
                    remediation_plans.c.dataset_id == dataset_id,
                    remediation_plans.c.shift_date == day.isoformat()).with_for_update()).mappings().one()
                plan_id = row['id']
        plan, result = _recompute_locked(connection, plan_id)
        revision = plan['revision'] + 1
        connection.execute(update(remediation_plans).where(remediation_plans.c.id == plan_id).values(
            result=_stamp(result, revision), revision=revision, state=State.completed.value))
        add_event(connection, actor, 'remediation', plan_id, now)
    return {'id': plan_id, 'revision': revision, 'result': _stamp(result, revision)}


def remediation_detail(plan_id):
    with engine.connect() as connection:
        plan, result = _recompute_read(connection, plan_id)
        claims = connection.execute(select(remediation_claims).where(remediation_claims.c.plan_id == plan_id)
                                    .order_by(remediation_claims.c.id)).mappings().all()
        published = connection.execute(select(remediation_dispatches).where(
            remediation_dispatches.c.plan_id == plan_id).order_by(remediation_dispatches.c.id)).mappings().all()
    result = _stamp(result, plan['revision'])
    result['id'] = plan_id
    result['published_at'] = plan['published']
    result['claims'] = [dict(row) for row in claims]
    result['published'] = [dict(row) for row in published]
    return result


def _stamp(result, revision):
    result = {**result, 'revision': revision}
    return result


def claim_remediation(plan_id, device, actor):
    now = time.time()
    with engine.begin() as connection:
        _, result = _recompute_locked(connection, plan_id)
        match = next((item for item in result['assignments']
                      if item['device'] == device and item['source'] == 'planned'), None)
        if match is None:
            existing = connection.execute(select(remediation_claims).where(
                remediation_claims.c.plan_id == plan_id,
                remediation_claims.c.device == device)).mappings().first()
            if existing is not None:
                return {'device': device, 'state': 'claimed', 'crew': existing['crew'],
                        'start_minute': existing['start_minute'], 'end_minute': existing['end_minute']}
            published = connection.execute(select(remediation_dispatches.c.id).where(
                remediation_dispatches.c.plan_id == plan_id,
                remediation_dispatches.c.device == device)).first()
            if published is not None:
                raise HTTPException(409, '该设备已发布派单，不能再接单。')
            raise HTTPException(409, '该设备当前不在可接单的排班建议中，请重算后再试。')
        savepoint = connection.begin_nested()
        try:
            connection.execute(insert(remediation_claims).values(
                plan_id=plan_id, device=device, zone=match['zone'], crew=match['crew'],
                start_minute=match['start_minute'], end_minute=match['end_minute'], owner=actor, created=now))
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            existing = connection.execute(select(remediation_claims).where(
                remediation_claims.c.plan_id == plan_id,
                remediation_claims.c.device == device)).mappings().first()
            if existing is None:
                raise
            match = existing
        add_event(connection, actor, 'claim', plan_id, now)
    return {'device': device, 'state': 'claimed', 'crew': match['crew'],
            'start_minute': match['start_minute'], 'end_minute': match['end_minute']}


def release_remediation(plan_id, device, actor):
    with engine.begin() as connection:
        _load_plan(connection, plan_id, lock=True)
        published = connection.execute(select(remediation_dispatches.c.id).where(
            remediation_dispatches.c.plan_id == plan_id, remediation_dispatches.c.device == device)).first()
        if published is not None:
            raise HTTPException(409, '该设备已发布派单，无法撤销接单。')
        removed = connection.execute(delete(remediation_claims).where(
            remediation_claims.c.plan_id == plan_id, remediation_claims.c.device == device))
        if not removed.rowcount:
            raise HTTPException(409, '该设备尚未接单。')
        add_event(connection, actor, 'release', plan_id, time.time())
    return {'device': device, 'state': 'released'}


def recompute_remediation(plan_id, actor):
    now = time.time()
    with engine.begin() as connection:
        plan, result = _recompute_locked(connection, plan_id)
        revision = plan['revision'] + 1
        stamped = _stamp(result, revision)
        connection.execute(update(remediation_plans).where(remediation_plans.c.id == plan_id).values(
            result=stamped, revision=revision, state=State.completed.value, owner=actor))
        add_event(connection, actor, 'recompute', plan_id, now)
    return {'id': plan_id, 'revision': revision, 'result': stamped}


def publish_remediation(plan_id, actor, expected_revision=None):
    """Persist the current proposed assignments as dispatches, idempotently.

    A retried publish re-reads the same revision and repeated devices are skipped
    by the (plan_id, device) unique key, so a failed-then-retried publish never
    issues a second order for the same device.
    """
    now = time.time()
    with engine.begin() as connection:
        plan = _load_plan(connection, plan_id, lock=True)
        if expected_revision is not None and plan['revision'] != expected_revision:
            raise HTTPException(409, '排班版本已更新，请刷新后重新发布。')
        plan, result = _recompute_locked(connection, plan_id)
        proposed = [item for item in result['assignments'] if item['source'] in ('planned', 'claimed')]
        existing = {row[0] for row in connection.execute(select(remediation_dispatches.c.device).where(
            remediation_dispatches.c.plan_id == plan_id))}
        created = 0
        for item in proposed:
            if item['device'] in existing:
                continue
            savepoint = connection.begin_nested()
            try:
                connection.execute(insert(remediation_dispatches).values(
                    plan_id=plan_id, device=item['device'], zone=item['zone'], crew=item['crew'],
                    start_minute=item['start_minute'], end_minute=item['end_minute'], source='planned',
                    revision=plan['revision'], created=now))
                savepoint.commit()
            except IntegrityError:
                savepoint.rollback()
                continue
            created += 1
        if created:
            connection.execute(update(remediation_plans).where(remediation_plans.c.id == plan_id).values(published=now))
            add_event(connection, actor, 'publish', plan_id, now)
    return {'id': plan_id, 'created': created, 'revision': plan['revision']}
