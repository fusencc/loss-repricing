from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, DecimalException, localcontext

from .types import Decision, DecisionStatus, OnlineState, Site, Snapshot, SourceLine


# 业务时间统一为当前中国标准时间，Windows 环境也可直接使用此固定时差。
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
REQUIRED_SOURCES = frozenset({"loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"})
PRICE_CONTEXT = Context(prec=50)
SITE_CURRENCIES: dict[Site, str] = {"MY": "MYR", "PH": "PHP", "SG": "SGD", "TH": "THB", "VN": "VND"}
SITE_ERRORS = {
    "MY": Decimal("0.02"),
    "PH": Decimal("0.02"),
    "SG": Decimal("0.02"),
    "TH": Decimal("0.02"),
    "VN": Decimal("2"),
}


def sales_window(now: datetime) -> tuple[date, date]:
    """取北京时间前七个完整自然日，起止日均包含，今天的部分销量不计入。"""
    if now.utcoffset() is None:
        raise ValueError("评估时间必须带时区")
    today = now.astimezone(BEIJING).date()
    return today - timedelta(days=7), today - timedelta(days=1)


def allowed_error(site: Site) -> Decimal:
    """已确认站点的实际价误差，单位为对应站点币。"""
    return SITE_ERRORS[site]


def evaluate(snapshot: Snapshot, now: datetime) -> list[Decision]:
    """纯计算，按在线子 SKU 产出一条判定，同一身份的仓配冲突不会产生两条提价建议。"""
    window = sales_window(now)
    today = now.astimezone(BEIJING).date()
    snapshot_problem = _snapshot_problem(snapshot, now, today)
    candidate_groups: dict[str, list[SourceLine]] = defaultdict(list)
    online_groups: dict[str, list[OnlineState]] = defaultdict(list)
    for row in snapshot.lines:
        candidate_groups[row.key.identity()].append(row)
    for state in snapshot.online:
        online_groups[state.key.identity()].append(state)

    results = []
    for identity, rows in candidate_groups.items():
        states = online_groups.get(identity, [])
        # 在固定十进制精度下统一换算；单行极端数值不能中断同批其他子 SKU。
        with localcontext(PRICE_CONTEXT):
            results.append(_decide(snapshot, now, window, snapshot_problem, rows, states))
    return results


def _snapshot_problem(snapshot: Snapshot, now: datetime, today: date) -> tuple[str, str] | None:
    # 待处理来源也必须完整，避免因漏源选了偏大的单件亏损。
    sources = snapshot.complete_sources
    if len(sources) != len(REQUIRED_SOURCES) or set(sources) != REQUIRED_SOURCES:
        return "incomplete_sources", "ERP 四份候选来源尚未完整齐备"
    if (
        snapshot.business_date != today
        or snapshot.as_of.astimezone(BEIJING).date() != today
        or snapshot.as_of > now
    ):
        return "stale_snapshot", "快照须在本北京时间业务日采集且采集时间有效"
    # 汇率口径为每 1 CNY 的币种单位。过期数据连同站币候选一起挂起。
    if snapshot.fx_date != today or snapshot.fx_rates.get("CNY") != Decimal("1"):
        return "invalid_fx", "缺少本业务日以 CNY 为基准的有效汇率"
    return None


def _decide(
    snapshot: Snapshot,
    now: datetime,
    window: tuple[date, date],
    snapshot_problem: tuple[str, str] | None,
    rows: list[SourceLine],
    states: list[OnlineState],
) -> Decision:
    key = rows[0].key
    contract = snapshot.contracts.get(key.site)
    row_ids = list(dict.fromkeys(row.row_id for row in rows if row.row_id.strip()))
    trace = {
        "business_date": snapshot.business_date.isoformat(),
        "evaluated_at": now.isoformat(),
        "as_of": snapshot.as_of.isoformat(),
        "fx_date": snapshot.fx_date.isoformat(),
        "fx_rates": {currency: str(rate) for currency, rate in snapshot.fx_rates.items()},
        "complete_sources": list(snapshot.complete_sources),
        "source_versions": dict(snapshot.source_versions),
        "source_rows": [row.model_dump(mode="json") for row in rows],
        "online_rows": [state.model_dump(mode="json") for state in states],
        "site_contract": contract.model_dump(mode="json") if contract else None,
        "sales_window": [day.isoformat() for day in window],
    }

    def finish(
        code: str,
        reason: str,
        *,
        status: DecisionStatus = "待处理",
        ready: bool = False,
        **prices,
    ) -> Decision:
        return Decision(
            key=key, status=status, ready=ready, reason_code=code, reason=reason,
            source_row_ids=row_ids, trace=trace, **prices,
        )

    if snapshot_problem:
        return finish(*snapshot_problem)

    # 异常记录保留来源证据，但不进入计算。按明细行 ID 在当前在线身份内消重。
    # 订单号与 SKU 并不能唯一标识行，同一订单内重复出现的子 SKU 仍可分别参与。
    deduped = {}
    for row in rows:
        if not row.row_id.strip():
            if row.abnormal:
                continue
            return finish("missing_row_id", "候选明细缺少原始行 ID")
        previous = deduped.setdefault(row.row_id, row)
        if previous != row and previous.model_dump(exclude={"source", "source_ref"}) != row.model_dump(
            exclude={"source", "source_ref"}
        ):
            return finish("conflicting_row", "同一在线子 SKU 的原始明细行在多份来源中内容冲突")

    excluded_rows = {row_id: "abnormal" for row_id, row in deduped.items() if row.abnormal}
    trace["excluded_source_rows"] = excluded_rows
    active_rows = [row for row in deduped.values() if not row.abnormal]
    if not active_rows:
        return finish("no_valid_loss", "候选明细均标为异常")
    key = active_rows[0].key
    identity_parts = (key.platform, key.shop_id, key.product_id, key.item_id, key.sku, key.warehouse)
    if not all(part.strip() for part in identity_parts):
        return finish("missing_mapping", "平台、店铺、产品、商品、子 SKU 或仓配映射缺失")
    if not states:
        return finish("missing_mapping", "候选明细未匹配到当前在线子 SKU")
    online = states[0]
    if any(state != online for state in states[1:]):
        return finish("conflicting_mapping", "同一在线子 SKU 存在多条冲突的在线快照")
    if any(row.key.warehouse != online.key.warehouse for row in active_rows):
        return finish("conflicting_warehouse", "候选仓配与在线快照不一致")
    if (
        not online.main_sku.strip()
        or any(row.main_sku != online.main_sku for row in active_rows)
    ):
        return finish("conflicting_main_sku", "候选主 SKU 缺失或与在线快照不一致")
    if not online.fee_scope.strip() or any(row.fee_scope != online.fee_scope for row in active_rows):
        return finish("conflicting_fee_scope", "候选费用口径缺失或与在线快照不一致")
    loss_currency = active_rows[0].currency
    if not loss_currency.strip() or any(row.currency != loss_currency for row in active_rows):
        return finish("conflicting_currency", "同一在线子 SKU 的候选亏损币种缺失或冲突")
    # 在线售价、一次毛利模型和上传价采用站点币。ERP 亏损原币独立校验并换汇。
    station_currency = SITE_CURRENCIES[key.site]
    trace["station_currency"] = station_currency
    if online.currency != station_currency:
        return finish("invalid_station_currency", f"在线币种与 {key.site} 站点币 {station_currency} 不一致")
    if online.observed_at.astimezone(BEIJING).date() != snapshot.business_date or online.observed_at > now:
        return finish("stale_online", "在线价格与保护数据须为本业务日有效快照")

    # 提交结果未知需继续锁定；72 小时只按已经确认生效的时间计算。
    if online.pending_submission:
        return finish("pending_submission", "已有未完成核验的提交")
    if not online.history_known:
        return finish("unknown_history", "缺少可确认的生效历史")
    if online.last_increase_at is not None and online.last_increase_at > now:
        return finish("invalid_history", "上次确认生效时间晚于评估时间")
    if online.protected:
        return finish("manual_protection", "命中人工保护名单", status="本轮不调")
    if online.current_margin >= 0:
        return finish("current_margin_nonnegative", "当前一次毛利已不亏损", status="本轮不调")
    if online.last_increase_at is not None:
        elapsed = now.astimezone(timezone.utc) - online.last_increase_at.astimezone(timezone.utc)
        if elapsed < timedelta(hours=72):
            return finish("recent_increase", "距离上次确认生效不足 72 小时", status="本轮不调")

    # 销量来源是同店同站的主 SKU 汇总。在线子 SKU 共用销量保护，独立取亏损值。
    if (
        not online.sales_complete
        or online.sales_7d is None
        or online.sales_7d < 0
        or (online.sales_start, online.sales_end) != window
    ):
        return finish("invalid_sales", "缺少前七个完整北京时间自然日的主 SKU 销量")
    if online.sales_7d >= 21:
        return finish("sales_protection", "主 SKU 前七个完整自然日销量达到 21 件", status="本轮不调")
    if online.activity_locked:
        return finish("activity_locked", "商品活动限制当前提价")
    if not online.sellable:
        return finish("not_sellable", "子 SKU 当前不可售")

    model = online.fee_model
    if model is None or not model.verified or not model.version.strip():
        return finish("unverified_fee_model", "缺少经确认并带版本的公司一次毛利模型")
    if (
        contract is None
        or contract.site != key.site
        or not contract.verified
        or contract.price_step <= 0
        or not contract.upload_column.strip()
        or not contract.version.strip()
    ):
        return finish("unverified_site_contract", "缺少经确认的站点上传字段、价格档位或合同版本")

    from_rate = snapshot.fx_rates.get(loss_currency)
    to_rate = snapshot.fx_rates.get(station_currency)
    if from_rate is None or from_rate <= 0 or to_rate is None or to_rate <= 0:
        return finish("invalid_fx", "亏损原币或站点币缺少正数汇率")

    retail = online.retail_price
    current = online.marketing_price if online.marketing_price is not None else retail
    if retail <= 0 or current <= 0 or current > retail:
        return finish("invalid_current_price", "当前含 VAT 零售价或实际营销价无效")

    try:
        valid_losses = {}
        for row in active_rows:
            # 原始亏损是负数。单件已按件计价，明细金额或可可靠分摊的整单金额按数量换算。
            if row.quantity <= 0:
                excluded_rows[row.row_id] = "invalid_quantity"
            elif row.loss_amount >= 0:
                excluded_rows[row.row_id] = "nonnegative_loss"
            elif row.basis == "order" and not row.allocation_reliable:
                excluded_rows[row.row_id] = "unreliable_order_allocation"
            else:
                magnitude = row.loss_amount.copy_abs()
                valid_losses[row.row_id] = magnitude if row.basis == "unit" else magnitude / row.quantity
        if not valid_losses:
            return finish("no_valid_loss", "没有可可靠归一到单件的负亏损明细")
        trace["candidate_unit_losses"] = {row_id: str(value) for row_id, value in valid_losses.items()}
        selected = min(valid_losses, key=lambda row_id: (valid_losses[row_id], row_id))
        unit_loss = valid_losses[selected]

        # 站点币亏损 = 原币亏损 / 原币每 CNY 单位 × 站点币每 CNY 单位。
        # 同币种换算精确为原值。跨币种先乘后除，减少中间的小数误差。
        station_loss = unit_loss if loss_currency == station_currency else unit_loss * to_rate / from_rate
        target_actual = current + station_loss
        discount = current / retail
        # 上传的是含 VAT 零售价。用价格比值反推并按站点步长舍入，可支持 0.025 等档位。
        raw_upload = target_actual * retail / current
        ticks = (raw_upload / contract.price_step).to_integral_value(rounding=contract.rounding)
        upload_price = ticks * contract.price_step

        # 用上传后零售价恢复实际售价，复算站点币误差与正常一次毛利。
        rounded_actual = upload_price * current / retail
        error = abs(rounded_actual - target_actual)
        tolerance = allowed_error(key.site)
        target_profit = model.profit(target_actual)
        rounded_profit = model.profit(rounded_actual)
    except DecimalException:
        return finish("invalid_calculation", "价格、汇率或费用模型超出可计算范围")

    trace.update({
        "loss_fx_rate": str(from_rate),
        "station_fx_rate": str(to_rate),
        "raw_upload_price": str(raw_upload),
        "rounded_actual": str(rounded_actual),
        "actual_price_error": str(error),
        "allowed_error": str(tolerance),
        "target_profit": str(target_profit),
    })
    prices = {
        "selected_row_id": selected, "unit_loss": unit_loss, "loss_currency": loss_currency,
        "station_loss": station_loss, "current_actual": current, "discount": discount,
        "target_actual": target_actual, "upload_price": upload_price,
        "predicted_profit": rounded_profit, "upload_column": contract.upload_column,
    }
    if upload_price <= 0 or error > tolerance:
        return finish("rounding_error", "站点价格档位导致实际价无效或超出误差容限", **prices)
    if target_profit < 0 or rounded_profit < 0:
        return finish("fee_shortfall", "直接补亏后的目标价或舍入后实际价仍亏损", **prices)
    return finish("ready", "补亏价格通过站点精度及正常一次毛利核验", ready=True, **prices)
