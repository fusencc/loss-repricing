"""持久任务队列：唯一键防止定时任务重复，条件更新防止重复领取。"""
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from sqlalchemy import select, update, or_, and_
from sqlalchemy.exc import IntegrityError
from app.models import Job, Setting, Audit
from app.db import transaction

BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULTS = dict(paused=False, paused_sites=[], pilot_shops=[], autonomous_enabled=False,
                site_enabled=[], schedule_enabled=False)


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


def parse_stamp(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc) if value else None


def settings_value(factory):
    with factory() as session:
        value = session.get(Setting, "runtime")
        return {**DEFAULTS, **(value.value if value else {})}


def update_settings(factory, changes, actor="admin"):
    with transaction(factory) as session:
        row = session.get(Setting, "runtime")
        value = {**DEFAULTS, **(row.value if row else {}), **changes}
        if row:
            row.value = value
        else:
            session.add(Setting(key="runtime", value=value))
        session.add(Audit(id=str(uuid4()), actor=actor, action="settings.update", detail=changes, created_at=stamp()))
    return value


def enqueue(factory, kind, payload, dedupe_key=None):
    job_id = str(uuid4())
    dedupe_key = dedupe_key or job_id
    try:
        with transaction(factory) as session:
            session.add(Job(id=job_id, kind=kind, payload=payload, dedupe_key=dedupe_key,
                            state="queued", lease_until=0, created_at=stamp()))
        return job_id
    except IntegrityError:
        with factory() as session:
            return session.scalar(select(Job.id).where(Job.dedupe_key == dedupe_key))


def claim_job(factory, owner, now):
    eligible = or_(and_(Job.state == "queued", Job.lease_until <= now),
                   and_(Job.state == "running", Job.lease_until < now))
    with transaction(factory) as session:
        job_id = session.scalar(select(Job.id).where(eligible).order_by(Job.created_at).limit(1))
        if not job_id:
            return None
        result = session.execute(update(Job).where(Job.id == job_id, eligible).values(
            state="running", owner=owner, lease_until=now + 120))
        return session.get(Job, job_id) if result.rowcount == 1 else None


def current_items(session):
    """全局运营视图：按批次创建时间及记录ID选当前目标，另保留全部未终结执行。

    旧记录的核验更新时间不能使其覆盖较新的评估；先选当前集合再按状态筛选。
    批次详情直接读取原记录，因此历史判定和追溯证据不丢失。
    """
    from app.models import Item, Batch, Execution
    from app.domain.types import VariantKey
    pending = set(session.scalars(select(Execution.item_id).where(
        Execution.state.in_(["submitting", "accepted", "unknown", "mismatch"]))))
    rows = session.scalars(select(Item).join(Batch).order_by(Batch.created_at.desc(), Item.id.desc()))
    seen, result = set(), []
    for item in rows:
        target = VariantKey.model_validate(item.key).target_identity()
        if target not in seen or item.id in pending:
            result.append(item)
        seen.add(target)
    return result
