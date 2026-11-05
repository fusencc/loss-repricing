"""人工与自动调价共用的价格基线；不从金额变化猜测操作者。

来源：评估快照的在线原始行。observed_at 是采集时间，每次轮询都会改变，
不属于价格版本。只有来源明确给出的 price_version/last_changed_at 才参与变更识别。
外部没有条件更新合同，本地检查不能替代上游原子写入；live另有独占写入门槛。
"""
from decimal import Decimal
from sqlalchemy import select, update
from app.db import transaction
from app.models import VariantGuard
from app.models.coordination import TargetControl
from app.services.coordination import get_control
from app.services.store import stamp

def _number(value):
    if value is None:
        return None
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text

def price_baseline(online):
    row = online.model_dump(mode="json") if hasattr(online, "model_dump") else online
    return {
        "retail_price": _number(row["retail_price"]),
        "marketing_price": _number(row.get("marketing_price")),
        "warehouse": row["key"]["warehouse"], "currency": row["currency"],
        "price_version": row.get("price_version"), "last_changed_at": row.get("last_changed_at"),
    }

def same_price(left, right):
    # 旧来源尚不提供版本时仍比较真实金额和仓配；有版本的来源不忽略版本变化。
    return all(left.get(k) == right.get(k) for k in
               ("retail_price", "marketing_price", "warehouse", "currency", "price_version", "last_changed_at"))

def lock_control(session, key):
    row = get_control(session, key, create=True)
    # SQLite开发库也取得写锁；MySQL按目标行锁定，与接管动作共用版本。
    session.execute(update(TargetControl).where(TargetControl.target_id == row.target_id)
                    .values(version=TargetControl.version))
    return session.scalar(select(TargetControl).where(TargetControl.target_id == row.target_id)
                          .with_for_update().execution_options(populate_existing=True))

def observe_prices(factory, snapshot):
    """自动评估前检查跨批外部变化，人工交回仅允许一次重新建立基线。"""
    authorizations={}
    with transaction(factory) as session:
        seen = set()
        for online in snapshot.online:
            ident = online.key.target_identity()
            if ident in seen:
                continue  # 映射冲突由领域层保留完整证据并阻断，不从重复行挑价。
            seen.add(ident)
            control = lock_control(session, online.key)
            guard = session.get(VariantGuard, ident)
            current = price_baseline(online)
            if guard and guard.execution_id:
                continue
            if not control.baseline or control.handback_pending:
                control.baseline, control.handback_pending = current, False
            elif control.mode == "auto" and not same_price(control.baseline, current):
                control.mode, control.reason = "review", "检测到外部价格、折扣、仓配或版本变化，等待负责人核对"
                control.changed_by, control.updated_at = "system", stamp()
                control.version += 1
            from app.services.coordination import lock_authorization,authorization_snapshot
            locked=lock_authorization(session,online.key)
            authorizations[ident]=authorization_snapshot(session,online.key,locked)
    return authorizations


def record_conflict(factory, key, reason):
    with transaction(factory) as session:
        row = lock_control(session, key)
        if row.mode == "auto":
            row.mode = "review"
        row.reason, row.changed_by, row.updated_at = reason, "system", stamp()
        row.version += 1

def verified_baseline(control, decision, actual_price):
    """本程序已核验后更新期望价；不改变接管状态，不伪造外部价格版本。"""
    rows = decision.trace.get("online_rows", [])
    if len(rows) != 1:
        return
    baseline = price_baseline(rows[0])
    baseline["retail_price"] = _number(decision.upload_price)
    if rows[0].get("marketing_price") is not None:
        baseline["marketing_price"] = _number(actual_price)
    # 外部更新后的版本只能由真实来源给出，保留旧值会导致下一轮保守挂起。
    # 此处不制造版本，也不把自身核验当作任何外部版本已确认。
    control.baseline = baseline
