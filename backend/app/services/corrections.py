"""人工修正沿用同一执行意图、防重锁和回读；批准不等于已改价。

与自动补亏不同，目标实际售价由有权销售员/主管明确提出。仍必须确认当前在线
身份、仓配、币种、保护、活动和费用模型，校验档位前后正常一次毛利不为负。
只跳过自动补亏的当前不亏/72小时筛选，不把人工修正伪造成历史亏损订单。
"""
from decimal import Decimal, DecimalException
from uuid import uuid4
from sqlalchemy import select
from app.db import transaction
from app.models import Batch, Item, Execution
from app.models.coordination import Correction, UserAccount
from app.services.coordination import Principal, principal, can_access, lock_authorization
from app.services.price_control import price_baseline, same_price, lock_control
from app.services.store import utcnow, stamp, BEIJING
from app.domain.engine import allowed_error, SITE_CURRENCIES, sales_window
from app.domain.money import MoneyRatio
from app.domain.types import Decision, VariantKey

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
    key, contract, model = online.key, snapshot.contracts.get(online.key.site), online.fee_model
    today = now.astimezone(BEIJING).date()
    if snapshot.business_date != today or snapshot.as_of > now:
        raise ValueError("修正须使用当日有效快照")
    if (online.observed_at > now or online.observed_at.astimezone(BEIJING).date() != today
            or (now-online.observed_at).total_seconds() > 300):
        raise ValueError("当前价格依据过期，请重新取数")
    if not online.history_known or online.pending_submission:
        raise ValueError("调价历史不完整或仍有未知提交")
    if (online.protected or not online.sales_complete or online.sales_7d is None
            or online.sales_7d < 0 or online.sales_7d >= 21
            or (online.sales_start, online.sales_end) != sales_window(now)):
        raise ValueError("人工修正仍需满足保护与完整销量依据")
    if online.activity_locked or not online.sellable:
        raise ValueError("活动锁价或不可售，先完成对应人工处理")
    if online.currency != SITE_CURRENCIES[key.site] or online.fee_scope != "normal":
        raise ValueError("币种或正常毛利口径不一致")
    if not model or not model.verified or not model.version or model.revenue_factor <= 0 or model.fixed_cost < 0:
        raise ValueError("缺少有效且已确认的正常毛利模型")
    if (not contract or not contract.verified or contract.site != key.site or not contract.version
            or not contract.upload_column or contract.price_step <= 0 or key.warehouse not in contract.warehouses):
        raise ValueError("站点仓配上传合同尚未确认")
    if not desired.is_finite() or desired <= 0 or online.retail_price <= 0:
        raise ValueError("实际售价必须为有限正数")
    current = online.marketing_price if online.marketing_price is not None else online.retail_price
    if current <= 0 or current > online.retail_price:
        raise ValueError("折扣数据无效")
    discount = MoneyRatio(current) / MoneyRatio(online.retail_price)
    target = MoneyRatio(desired)
    upload = (target / discount).round_to_step(contract.price_step, contract.rounding)
    actual = MoneyRatio(upload) * discount
    profit = model.profit_exact(actual)
    if model.profit_exact(target).numerator < 0 or profit.numerator < 0:
        raise ValueError("修正目标或档位后的正常一次毛利仍为负")
    if upload <= 0 or (actual-target).absolute() > MoneyRatio(allowed_error(key.site)):
        raise ValueError("修正价按站点档位舍入后超出实际售价误差")
    return Decision(key=key,status="待处理",ready=True,reason_code="manual_correction",
        reason="已授权人工修正："+reason,source_row_ids=[],current_actual=current,discount=discount.decimal(),
        target_actual=desired,upload_price=upload,predicted_profit=profit.decimal(),upload_column=contract.upload_column,
        trace={"formula_version":"manual-correction-v1","correction_id":correction_id,
               "business_date":today.isoformat(),"evaluated_at":now.isoformat(),
               "online_rows":[online.model_dump(mode="json")],"site_contract":contract.model_dump(mode="json"),
               "source_versions":dict(snapshot.source_versions),"rounded_actual":str(actual.decimal()),
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
            old_item_id = execution.item_id if execution else None
        else:
            old_item_id = None
        if not existing_id and correction.state != "queued":
            return None
        original = session.get(Item, correction.item_id)
        key = VariantKey.model_validate(original.key)
    if existing_id:
        service.reconcile(item_id=old_item_id)
        return None
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
    with transaction(factory) as session:
        current=session.get(Correction,correction_id)
        execution=session.get(Execution,current.execution_id) if current.execution_id else None
        if not execution or execution.state=="failed":
            item=session.get(Item,item_id)
            current.state,current.updated_at,current.result="failed",stamp(),{"message":item.reason}
    service._summarize(batch_id)
    return batch_id
