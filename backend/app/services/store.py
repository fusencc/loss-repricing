"""持久任务队列：唯一键防止定时任务重复，条件更新防止重复领取。"""
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from sqlalchemy import select, update, or_, and_
from sqlalchemy.exc import IntegrityError
from app.models import Audit, Batch, Execution, Item, Job, Setting
from app.db import transaction
from app.observability import current_context, event, valid_id

BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
ERP_RECONCILIATION_PREFIX = "erp-preview:"
DEFAULTS = dict(paused=False, paused_sites=[], pilot_shops=[], autonomous_enabled=False,
                site_enabled=[], schedule_enabled=False)


def is_erp_reconciliation(batch):
    return batch.run_key.startswith(ERP_RECONCILIATION_PREFIX)


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


def parse_stamp(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc) if value else None


def _runtime_value(row):
    return {**DEFAULTS, **(row.value if row else {})}


def settings_value(factory):
    with factory() as session:
        return _runtime_value(session.get(Setting, "runtime"))


def settings_for_submit(session):
    # MySQL当前读与设置写入串行化；共享锁允许独立目标同时授权。
    # SQLite协作模式在此之前已用lock_control取得写锁。
    row = session.get(Setting, "runtime", with_for_update={"read": True}, populate_existing=True)
    return _runtime_value(row)


def _lock_runtime_settings(session):
    if session.get_bind().dialect.name == "sqlite":
        # SQLite忽略FOR UPDATE，先取得写锁，再读取和合并，防止并发设置互相覆盖。
        session.execute(update(Setting).where(Setting.key == "runtime").values(value=Setting.value))
    if session.get(Setting, "runtime") is None:
        # 首次设置允许并发创建；只回滚这次插入，不回滚同一事务的核验证据。
        try:
            with session.begin_nested():
                session.add(Setting(key="runtime", value={}))
                session.flush()
        except IntegrityError:
            pass
    return session.get(Setting, "runtime", with_for_update=True, populate_existing=True)


def _apply_settings(session, row, changes, actor):
    row.value = {**_runtime_value(row), **changes}
    session.add(Audit(id=str(uuid4()), actor=actor, action="settings.update", detail=changes, created_at=stamp()))
    return row.value


def update_settings(factory, changes, actor="admin"):
    with transaction(factory) as session:
        return _apply_settings(session, _lock_runtime_settings(session), changes, actor)


def pause_site(session, site):
    row = _lock_runtime_settings(session)
    paused_sites = _runtime_value(row)["paused_sites"]
    if site not in paused_sites:
        _apply_settings(session, row, {"paused_sites": sorted([*paused_sites, site])}, "worker")


def enqueue(factory, kind, payload, dedupe_key=None, *, retry_failed=False):
    job_id = str(uuid4())
    dedupe_key = dedupe_key or job_id
    # 仅保存技术关联编号，不改变任务类型、唯一键或业务负载。旧任务无此字段仍可领取。
    payload = dict(payload)
    context = {key: value for key, value in current_context().items()
               if key in {'request_id', 'operation_id'} and valid_id(value)}
    if context:
        payload['_log_context'] = context
    else:
        payload.pop('_log_context', None)
    try:
        with transaction(factory) as session:
            session.add(Job(id=job_id, kind=kind, payload=payload, dedupe_key=dedupe_key,
                            state="queued", lease_until=0, created_at=stamp()))
        event('job.queued', job_id=job_id, kind=kind)
        return job_id
    except IntegrityError:
        with transaction(factory) as session:
            if retry_failed:
                session.execute(update(Job).where(Job.dedupe_key == dedupe_key, Job.state == "failed")
                                .values(state="queued", owner=None, lease_until=0, error=None))
            return session.scalar(select(Job.id).where(Job.dedupe_key == dedupe_key))


def claim_job(factory, owner, now):
    eligible = or_(and_(Job.state == "queued", Job.lease_until <= now),
                   and_(Job.state == "running", Job.lease_until < now))
    with transaction(factory) as session:
        job = session.execute(select(Job.id, Job.payload).where(eligible).order_by(Job.created_at).limit(1)).first()
        if not job:
            return None
        payload = dict(job.payload, _attempt_started=stamp(datetime.fromtimestamp(now, timezone.utc)))
        result = session.execute(update(Job).where(Job.id == job.id, eligible).values(
            state="running", owner=owner, lease_until=now + 120, payload=payload))
        return session.get(Job, job.id) if result.rowcount == 1 else None


def write_decisions(session, batch_id, decisions, *, ready_hold=None):
    existing = {row.variant_id: row for row in session.scalars(select(Item).where(Item.batch_id == batch_id))}
    # 发送前取消的意图也属于执行历史，其商品阶段可能是 blocked。
    executed = set(session.scalars(
        select(Execution.item_id).join(Item).where(Item.batch_id == batch_id)
    )) if existing else set()
    seen = set()
    for decision in decisions:
        identity = decision.key.identity()
        seen.add(identity)
        item = existing.get(identity)
        if item and (item.id in executed or item.phase in {
            "submitting", "accepted", "unknown", "verified", "uploaded", "mismatch", "failed",
        }):
            continue
        if item is None:
            item = Item(id=str(uuid4()), batch_id=batch_id, variant_id=identity,
                        key=decision.key.model_dump(mode="json"))
            session.add(item)
        item.decision = decision.model_dump(mode="json")
        item.status, item.reason = decision.status, decision.reason
        item.phase = "proposed" if decision.ready else "excluded" if decision.status == "不调价" else "blocked"
        if decision.ready and ready_hold is not None:
            item.phase, item.reason = "blocked", ready_hold
        item.updated_at = stamp()
    for identity, item in existing.items():
        if identity not in seen and item.id not in executed and item.phase in {"proposed", "blocked", "excluded"}:
            item.status, item.phase, item.reason = "不调价", "excluded", "重新取数后已不在当前亏损候选中"
            item.updated_at = stamp()


def current_items(session):
    """全局运营视图：最新目标记录，加上全部未终结执行；状态筛选在投影后执行。

    唯一的历史结果提升情形：最新记录仅因已有提交待核验而挂起，且这个依赖后来
    已终结。此时展示真实核验/失败记录，不修改旧批次，也不掩盖较新的业务异常。
    """
    from collections import defaultdict
    from app.domain.types import VariantKey
    executions = {execution.item_id: execution for execution in session.scalars(select(Execution))}
    pending_states = {"submitting", "accepted", "unknown", "mismatch"}
    groups = defaultdict(list)
    # 显式历史对账只在批次页查看，不能替换当日运营记录或覆盖待核验项。
    operational = select(Item).join(Batch).where(~Batch.run_key.startswith(ERP_RECONCILIATION_PREFIX))
    for item in session.scalars(operational.order_by(Batch.created_at.desc(), Item.id.desc())):
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
                        and executions[row.id].state in {"verified", "uploaded", "failed"}
                        and parse_stamp(executions[row.id].created_at) <= observed
                        <= parse_stamp(executions[row.id].updated_at)]
            if resolved:
                latest = max(resolved, key=lambda row: (executions[row.id].updated_at,
                                                       executions[row.id].created_at, row.id))
        result.append(latest)
        # 多个历史未知执行必须逐个显示；一个已结束不代表其他请求也已结束。
        result.extend(row for row in pending if row.id != latest.id)
    return result
