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
    """全局运营视图：最新目标记录，加上全部未终结执行；状态筛选在投影后执行。

    唯一的历史结果提升情形：最新记录仅因已有提交待核验而挂起，且这个依赖后来
    已终结。此时展示真实核验/失败记录，不修改旧批次，也不掩盖较新的业务异常。
    """
    from collections import defaultdict
    from app.models import Item, Batch, Execution
    from app.domain.types import VariantKey
    executions = {execution.item_id: execution for execution in session.scalars(select(Execution))}
    pending_states = {"submitting", "accepted", "unknown", "mismatch"}
    groups = defaultdict(list)
    for item in session.scalars(select(Item).join(Batch).order_by(Batch.created_at.desc(), Item.id.desc())):
        groups[VariantKey.model_validate(item.key).target_identity()].append(item)
    result = []
    for rows in groups.values():
        latest = rows[0]
        pending = [row for row in rows if row.id in executions and executions[row.id].state in pending_states]
        if (not pending and latest.id not in executions and latest.phase == "blocked"
                and latest.decision.get("reason_code") == "pending_submission"):
            # 旧记录没有显式依赖ID，以评估时刻还原依赖：请求在该时刻已存在，
            # 核验在其后终结。更早已结束的本地请求不能解释外部来源的未知提交。
            observed = parse_stamp(latest.decision.get("trace", {}).get("evaluated_at") or latest.updated_at)
            resolved = [row for row in rows if row.id in executions
                        and executions[row.id].state in {"verified", "failed"}
                        and parse_stamp(executions[row.id].created_at) <= observed
                        <= parse_stamp(executions[row.id].updated_at)]
            if resolved:
                latest = max(resolved, key=lambda row: (executions[row.id].updated_at,
                                                       executions[row.id].created_at, row.id))
        result.append(latest)
        # 多个历史未知执行必须逐个显示；一个已结束不代表其他请求也已结束。
        result.extend(row for row in pending if row.id != latest.id)
    return result
