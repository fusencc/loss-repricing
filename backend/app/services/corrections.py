"""人工修正沿用同一执行意图、防重锁和回读；批准不等于已改价。

与自动补亏不同，目标实际售价由有权销售员/主管明确提出。仍必须确认当前在线
身份、仓配、币种、保护、活动和费用模型，校验档位前后库存对应完整在线利润率至少1%。
人工修正不要求关联 ERP 亏损订单，可跳过自动72小时冷却；不把修正价伪造成历史亏损金额。
"""
from decimal import Decimal, DecimalException
from uuid import uuid4
from sqlalchemy import select
from app.db import transaction
from app.models import Batch, Item, Execution
from app.models.coordination import Correction, UserAccount
from app.services.coordination import Principal, principal, can_access, lock_authorization
from app.services.price_control import price_baseline, same_price, lock_control
from app.services.store import utcnow, stamp, BEIJING, is_erp_reconciliation
from app.domain.engine import allowed_error, conflicting_main_sku_scopes, SITE_CURRENCIES
from app.domain.money import MoneyRatio
from app.domain.types import Decision, OnlineSourceIssue, VariantKey, ONLINE_PROFIT_MINIMUM_MARGIN

def correction_gate(session, decision, control, locked=None):
    correction = session.get(Correction, decision.trace.get("correction_id"))
    if (not correction or correction.target_id != decision.key.target_identity()
            or correction.state not in {"queued", "executing"}):
        return False, "人工修正任务已失效或不属于本目标"
    if control.mode != "manual" or control.version != correction.control_version:
        return False, "接管状态或版本已变化，原修正方案失效"
    for identity, approval in [(correction.requested_by, False), (correction.approved_by, True)]:
        if identity == "admin":
            actor = Principal("admin", "admin", "管理员", "admin", True)
        else:
            user = (locked.users.get(identity) if locked is not None else session.get(UserAccount, identity)) if identity else None
            if not user or not user.active:
                return False, "修正操作人已停用或缺少有效批准"
            actor = principal(user)
        if not can_access(session, actor, decision.key,locked=locked):
            return False, "商品归属已变化，原修正操作人不再有权限"
        if approval and actor.role not in {"admin", "manager"} and not actor.can_correct:
            return False, "批准人的修正执行权限已撤销"
    return True, ""

def manual_decision(snapshot, online, desired, reason, correction_id, now):
    key, contract = online.key, snapshot.contracts.get(online.key.site)
    today = now.astimezone(BEIJING).date()
    if snapshot.business_date != today or snapshot.as_of > now:
        raise ValueError("修正须使用当日有效快照")
    if (online.observed_at > now or online.observed_at.astimezone(BEIJING).date() != today
            or (now-online.observed_at).total_seconds() > 300):
        raise ValueError("当前价格依据过期，请重新取数")
    if online.last_changed_at is not None and online.last_changed_at > online.observed_at:
        raise ValueError("价格变更时间晚于该价格的观察时间")
    target = key.target_identity()
    scope = key.sales_scope(online.main_sku)
    if any(
        isinstance(issue, OnlineSourceIssue)
        and (issue.key.target_identity() == target or scope in issue.sales_scopes())
        for issue in snapshot.source_issues
    ):
        raise ValueError("目标或共享主 SKU 的在线数据异常，需核对后重新取数")
    if scope in conflicting_main_sku_scopes(snapshot.online, now):
        raise ValueError("共享主 SKU 的在线归属存在冲突，需核对后重新取数")
    if online.pending_submission:
        raise ValueError("仍有未知提交")
    if online.protected:
        raise ValueError("命中人工保护名单")
    # 2026-11-20用户暂停活动限制，人工修正与常规定价使用相同口径。
    if online.currency != SITE_CURRENCIES[key.site] or online.fee_scope != "normal":
        raise ValueError("币种或亏损来源口径不一致")
    if (not contract or not contract.verified or contract.site != key.site or not contract.version
            or not contract.upload_column or contract.price_step <= 0 or key.warehouse not in contract.warehouses):
        raise ValueError("站点仓配上传合同尚未确认")
    if not desired.is_finite() or desired <= 0 or online.retail_price <= 0:
        raise ValueError("实际售价必须为有限正数")
    current = online.marketing_price if online.marketing_price is not None else online.retail_price
    if current <= 0 or current > online.retail_price:
        raise ValueError("折扣数据无效")
    discount = MoneyRatio(current) / MoneyRatio(online.retail_price)
    profit_check = online.profit_check
    if profit_check is None:
        raise ValueError("缺少库存分流、部门费率及完整在线利润公式")
    if problem := profit_check.problem(now, today):
        raise ValueError(problem)
    target = MoneyRatio(desired)
    upload = (target / discount).round_to_step(contract.price_step, contract.rounding)
    actual = MoneyRatio(upload) * discount
    if (not profit_check.model.meets_minimum_online_margin(target)
            or not profit_check.model.meets_minimum_online_margin(actual)):
        raise ValueError("人工指定价的在线利润率未达到1%，请修正价格")
    profit = profit_check.model.profit_exact(actual)
    if upload <= 0 or (actual-target).absolute() > MoneyRatio(allowed_error(key.site)):
        raise ValueError("修正价按站点档位舍入后超出实际售价误差")
    return Decision(key=key,status="待处理",ready=True,reason_code="manual_correction",
        reason="已授权人工修正："+reason,source_row_ids=[],current_actual=current,discount=discount.decimal(),
        target_actual=desired,upload_price=upload,predicted_profit=profit.decimal(),upload_column=contract.upload_column,
        trace={"formula_version":"manual-online-margin-missing-stock-direct-v4","minimum_margin":str(ONLINE_PROFIT_MINIMUM_MARGIN),"correction_id":correction_id,
               "business_date":today.isoformat(),"evaluated_at":now.isoformat(),
               "online_rows":[online.model_dump(mode="json")],"site_contract":contract.model_dump(mode="json"),
               "source_versions":dict(snapshot.source_versions),"rounded_actual":str(actual.decimal()),
               "warning_codes":(["unknown_history"] if not online.history_known else []) + (["not_sellable"] if not online.sellable else []),
               "warnings":online.profit_check.warnings() + (["近72小时调价历史无法确认，仅告警"] if not online.history_known else []) + (["子 SKU 当前不可售，仅告警"] if not online.sellable else []),
               "exact_target_actual":{"numerator":str(desired),"denominator":"1"},
               "manual_reason":reason})

def run_correction(service, correction_id):
    factory = service.factory
    with factory() as session:
        correction = session.get(Correction, correction_id)
        if not correction:
            raise ValueError("修正方案不存在")
        existing_id = correction.execution_id
        if existing_id:
            execution = session.get(Execution, existing_id)
            if execution is None:
                raise ValueError("修正关联的执行记录不存在")
            old_item = session.get(Item, execution.item_id)
            execution_batch = session.get(Batch, old_item.batch_id)
            if (execution_batch.mode != service.adapter.mode
                    or execution_batch.run_key != "correction:" + correction_id):
                raise ValueError("修正关联的执行批次不匹配")
            old_item_id = old_item.id
            execution_batch_id = execution_batch.id
        if not existing_id and correction.state != "queued":
            return None
        original = session.get(Item, correction.item_id)
        source_batch = session.get(Batch, original.batch_id)
        # 已排队方案也必须遵循原始批次隔离，切换适配器不能沿用旧环境的人工授权。
        if source_batch.mode != service.adapter.mode:
            raise ValueError("修正来源批次模式不一致")
        if is_erp_reconciliation(source_batch):
            raise ValueError("文件对账批次仅供查阅")
        key = VariantKey.model_validate(original.key)
    if existing_id:
        # 恢复任务复用原执行及批次，worker 仍可把任务链接指向执行详情。
        service.reconcile(item_id=old_item_id)
        _finish_correction(service, correction_id, old_item_id, execution_batch_id)
        return execution_batch_id
    today = utcnow().astimezone(BEIJING).date()
    snapshot = service._enrich(service.adapter.collect(today))
    # 提案存的是用户看到的原价；重新取数只能复核，不能替换依据后继续执行旧授权。
    try:
        states = [o for o in snapshot.online if o.key.target_identity() == key.target_identity()]
        if len(states) != 1:
            raise ValueError("在线目标缺失或映射冲突")
        rows = correction.baseline.get("online_rows", [])
        if len(rows) != 1 or not same_price(price_baseline(rows[0]), price_baseline(states[0])):
            raise ValueError("外部价格、折扣、仓配或版本已变化，修正方案失效")
        decision = manual_decision(snapshot,states[0],Decimal(correction.desired_actual),
                                   correction.reason,correction.id,utcnow())
        with transaction(factory) as session:
            control = lock_control(session,key)
            locked=lock_authorization(session,key,[correction.requested_by,correction.approved_by])
            allowed, reason = correction_gate(session,decision,control,locked=locked)
            if not allowed:
                raise ValueError(reason)
        allowed, reason = service._allowed(key,manual=True)
        if not allowed:
            raise ValueError(reason)
    except (ValueError, DecimalException) as exc:
        with transaction(factory) as session:
            current = session.get(Correction,correction_id)
            if current.state == "queued":
                current.state, current.updated_at = "stale", stamp()
                current.result = {"message":str(exc)}
        return None
    batch_key = "correction:"+correction_id
    with transaction(factory) as session:
        batch = session.scalar(select(Batch).where(Batch.run_key == batch_key))
        if batch is None:
            batch = Batch(id=str(uuid4()),run_key=batch_key,business_date=today.isoformat(),mode=service.adapter.mode,
                status="running",created_at=stamp(),snapshot=snapshot.model_dump(mode="json"),summary={})
            session.add(batch); session.flush()
        elif batch.mode != service.adapter.mode:
            raise ValueError("修正批次模式不一致")
        batch_id=batch.id
    service._save_decisions(batch_id,[decision])
    with factory() as session:
        item_id=session.scalar(select(Item.id).where(Item.batch_id==batch_id))
    if service._submit(item_id,decision,today) is False:
        with transaction(factory) as session:
            row=session.get(Correction,correction_id)
            row.state,row.result,row.updated_at="stale",{"message":"已跨日，请重新取数提交修正"},stamp()
    service.reconcile(item_id=item_id)
    _finish_correction(service, correction_id, item_id, batch_id)
    return batch_id


def _finish_correction(service, correction_id, item_id, batch_id):
    with transaction(service.factory) as session:
        correction = session.get(Correction, correction_id, with_for_update=True)
        execution = session.get(Execution, correction.execution_id) if correction.execution_id else None
        # 明确拒绝已在提交阶段落库，恢复时从执行记录补齐方案结果。
        failed = correction.state in {"queued", "executing"} and (execution is None or execution.state == "failed")
        if failed:
            item = session.get(Item, item_id)
            correction.state = "failed"
            correction.updated_at = stamp()
            correction.result = {"message": item.reason}
        if failed or session.get(Batch, batch_id).status == "running":
            service._write_batch_summary(session, batch_id)
