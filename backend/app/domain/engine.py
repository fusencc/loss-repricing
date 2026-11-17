from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException, localcontext
from itertools import chain

from .money import MoneyRatio, PRICE_CONTEXT

from .types import (
    CandidateSourceIssue, Decision, DecisionStatus, OnlineState, Site, Snapshot,
    SourceIssue, SourceLine, VariantKey,
)


# 业务时间统一为当前中国标准时间，Windows 环境也可直接使用此固定时差。
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
REQUIRED_SOURCES = frozenset({"loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"})
# 依据：已确认设计“数据与算价说明”；完整变量/单位见 docs/calculation-and-sources.md。
# 11/13改用自有历史、网页七日指标和指定固定费率；金额规则仍为补亏后保本。
# 规则版本不是公司费率或接口验收标志，固定表另存内容版本。
FORMULA_VERSION = "loss-local-history-web-sales-20261113-v3"
# 保存/展示为50位有效数字；档位、容限、毛利正负以 Decimal 精确比值判定。
# 不能先把循环小数近似为50位，再用其决定整档/半档或毛利是否为零。
SITE_CURRENCIES: dict[Site, str] = {"MY": "MYR", "PH": "PHP", "SG": "SGD", "TH": "THB", "VN": "VND"}
SITE_ERRORS = {
    "MY": Decimal("0.02"),
    "PH": Decimal("0.02"),
    "SG": Decimal("0.02"),
    "TH": Decimal("0.02"),
    "VN": Decimal("2"),
}


def valid_sales_observation(state: OnlineState, now: datetime) -> bool:
    """用户指定销售网页七日指标；同业务日取得完整主SKU汇总才可用于21件保护。"""
    observed = state.sales_observed_at
    return (state.sales_complete and state.sales_7d is not None and state.sales_7d >= 0
            and state.sales_basis == "sales_system_web_sku_seven_days" and observed is not None
            and observed <= now and observed.astimezone(BEIJING).date() == now.astimezone(BEIJING).date())


def conflicting_sales_scopes(
    online: Iterable[OnlineState], now: datetime,
) -> set[tuple[str, str, Site, str]]:
    """同店同站主 SKU 的销量为共享汇总，核对完整窗口数值与当日商品归属。"""
    today = now.astimezone(BEIJING).date()
    counts = defaultdict(set)
    scopes_by_target = defaultdict(set)
    for state in online:
        if (not state.main_sku.strip() or state.observed_at > now
                or state.observed_at.astimezone(BEIJING).date() != today):
            continue
        scope = state.key.sales_scope(state.main_sku)
        scopes_by_target[state.key.target_identity()].add(scope)
        if valid_sales_observation(state, now):
            counts[scope].add(state.sales_7d)
    conflicted = {scope for scope, values in counts.items() if len(values) > 1}
    for scopes in scopes_by_target.values():
        if len(scopes) > 1:
            # 同一上传对象被关联到不同主 SKU，两组共享销量都不能作为保护依据。
            conflicted.update(scopes)
    return conflicted


def allowed_error(site: Site) -> Decimal:
    """已确认站点的实际价误差，单位为对应站点币。"""
    return SITE_ERRORS[site]


def _order_scope(row: SourceLine | CandidateSourceIssue) -> tuple[str, str, str]:
    return row.key.platform, row.key.shop_id, row.order_id


def _decision_key(group, states) -> VariantKey | None:
    reference = next((row.key for row in group if row.key.warehouse is not None), None)
    if reference is None:
        # 有唯一完整在线键时，用它展示挂起对象。来源仓配仍在异常记录中保持空值。
        known_keys = {state.key.warehouse: state.key for state in states}
        if len(known_keys) != 1:
            return None
        reference = next(iter(known_keys.values()))
    return reference if isinstance(reference, VariantKey) else VariantKey.model_validate(reference.model_dump())


def evaluate(snapshot: Snapshot, now: datetime) -> list[Decision]:
    """纯计算，按在线子 SKU 产出一条判定，同一身份的仓配冲突不会产生两条提价建议。"""
    today = now.astimezone(BEIJING).date()
    batch_problem = snapshot_problem(snapshot, now, today)
    conflicted_sales = conflicting_sales_scopes(snapshot.online, now)
    candidate_groups: dict[str, list[SourceLine | CandidateSourceIssue]] = defaultdict(list)
    online_groups: dict[str, list[OnlineState]] = defaultdict(list)
    order_lines = defaultdict(set)
    detail_mappings = defaultdict(set)
    issues_by_identity = defaultdict(list)
    first_issue_by_target = {}
    first_issue_by_detail = {}
    first_issue_by_sales = {}
    candidate_issues = []
    for index, issue in enumerate(snapshot.source_issues):
        issues_by_identity[issue.key.identity()].append(index)
        first_issue_by_target.setdefault(issue.key.target_identity(), index)
        if isinstance(issue, CandidateSourceIssue):
            candidate_issues.append(issue)
            first_issue_by_detail.setdefault((*_order_scope(issue), issue.row_id), index)
        else:
            for scope in issue.sales_scopes():
                first_issue_by_sales.setdefault(scope, index)
    for row in chain(snapshot.lines, candidate_issues):
        identity = row.key.identity()
        candidate_groups[identity].append(row)
        # 跨来源的同一明细只记一次。一单多明细仍保留，供整单亏损归属交叉检查。
        scope = _order_scope(row)
        order_lines[scope].add((identity, row.row_id))
        if row.order_id.strip() and row.row_id.strip():
            detail_mappings[(*scope, row.row_id)].add(identity)
    # 同一个来源明细只归一个在线变体。先于分组内去重检查，避免两组各自算价。
    conflicting_mappings = {
        identity for identities in detail_mappings.values() if len(identities) > 1 for identity in identities
    }
    multi_line_orders = {scope for scope, details in order_lines.items() if len(details) > 1}
    for state in snapshot.online:
        online_groups[state.key.identity()].append(state)

    # 执行目标与来源匹配分开：同一在线商品若同时对应多个来源SKU，全部挂起。
    # 不能移除SKU后直接取最小亏损，否则不同来源映射的样本会被混算。
    aliases = defaultdict(set)
    for row in chain(snapshot.lines, snapshot.online, snapshot.source_issues):
        aliases[row.key.target_identity()].add(row.key.identity())
    results = []
    for identity, group in candidate_groups.items():
        states = online_groups.get(identity, [])
        key = _decision_key(group, states)
        if key is None:
            continue  # 完整在线身份未知。来源异常留在批次，仍参与前面的订单归属核对。
        target = key.target_identity()
        rows = [row for row in group if isinstance(row, SourceLine)]
        # 全量异常保存在快照。商品保留自己的记录，关联目标、订单明细和共享主 SKU 各引用一条，
        # 避免每个商品都保存同一组共享异常。引用保留原来源和记录序号。
        issue_links = dict.fromkeys(issues_by_identity.get(identity, ()))
        sales_issue = next((
            index for row in chain(rows, states)
            if (index := first_issue_by_sales.get(row.key.sales_scope(row.main_sku))) is not None
        ), None)
        detail_issue = next((
            index for row in group
            if (index := first_issue_by_detail.get((*_order_scope(row), row.row_id))) is not None
        ), None)
        for relation, index in (
            ("same_target", first_issue_by_target.get(target)),
            ("shared_main_sku", sales_issue),
            ("same_order_detail", detail_issue),
        ):
            if index is not None:
                issue_links.setdefault(index, relation)
        issues = [(snapshot.source_issues[index], relation) for index, relation in issue_links.items()]
        # 在固定十进制精度下统一换算；单行极端数值不能中断同批其他子 SKU。
        with localcontext(PRICE_CONTEXT):
            problem = batch_problem
            if len(aliases[target]) > 1:
                problem = ("ambiguous_target_alias", "同一在线商品存在多个来源SKU映射，需先确认唯一映射")
            elif identity in conflicting_mappings:
                problem = ("conflicting_detail_mapping", "同一订单明细映射到多个在线变体，需核对来源")
            elif issues:
                problem = ("source_record_invalid", "来源记录字段异常，需核对关联明细或在线主 SKU")
            elif conflicted_sales and any(
                row.key.sales_scope(row.main_sku) in conflicted_sales for row in chain(rows, states)
            ):
                problem = ("conflicting_main_sku_sales", "共享主 SKU 的在线归属或七日销量存在冲突")
            results.append(_decide(snapshot, now, problem, key, rows, states, multi_line_orders, issues))
    return results


def snapshot_problem(snapshot: Snapshot, now: datetime, today: date) -> tuple[str, str] | None:
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
    # 汇率单位由fx_rate_unit明确；两种报价的CNY基准都须为1。过期数据挂起。
    if snapshot.fx_date != today or snapshot.fx_rates.get("CNY") != Decimal("1"):
        return "invalid_fx", "缺少本业务日以 CNY 为基准的有效汇率"
    return None


def _decide(
    snapshot: Snapshot,
    now: datetime,
    snapshot_problem: tuple[str, str] | None,
    key: VariantKey,
    rows: list[SourceLine],
    states: list[OnlineState],
    multi_line_orders: set[tuple[str, str, str]],
    issues: list[tuple[SourceIssue, str | None]],
) -> Decision:
    contract = snapshot.contracts.get(key.site)
    issue_rows = (issue for issue, relation in issues if relation is None and isinstance(issue, CandidateSourceIssue))
    row_ids = list(dict.fromkeys(row.row_id for row in chain(rows, issue_rows) if row.row_id.strip()))
    trace = {
        "formula_version": FORMULA_VERSION,
        "arithmetic": "decimal-ratio-v1",
        "decimal_precision": PRICE_CONTEXT.prec,
        "business_date": snapshot.business_date.isoformat(),
        "evaluated_at": now.isoformat(),
        "as_of": snapshot.as_of.isoformat(),
        "fx_date": snapshot.fx_date.isoformat(),
        "fx_rates": {currency: str(rate) for currency, rate in snapshot.fx_rates.items()},
        "fx_rate_unit": snapshot.fx_rate_unit,
        "complete_sources": list(snapshot.complete_sources),
        "source_versions": dict(snapshot.source_versions),
        "source_rows": [row.model_dump(mode="json") for row in rows],
        "online_rows": [state.model_dump(mode="json") for state in states],
        "site_contract": contract.model_dump(mode="json") if contract else None,
        "sales_basis": "sales_system_web_sku_seven_days",
    }
    if issues:
        issue_trace = []
        for issue, relation in issues:
            if relation is None:
                issue_trace.append(issue.model_dump(mode="json"))
            else:
                issue_trace.append({
                    "source": issue.source,
                    "record_number": issue.record_number,
                    "key": issue.key.model_dump(mode="json"),
                    "relation": relation,
                })
        trace["source_issues"] = issue_trace

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
    if online.last_changed_at is not None and online.last_changed_at > online.observed_at:
        return finish("stale_online", "价格变更时间晚于该价格的观察时间")

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
    if not valid_sales_observation(online, now):
        return finish("invalid_sales", "缺少本业务日完整的销售网页主SKU七日销量")
    if online.sales_7d >= 21:
        return finish("sales_protection", "主 SKU 销售网页七日销量达到 21 件", status="本轮不调")
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
    if key.warehouse not in contract.warehouses:
        return finish("unsupported_warehouse", "当前仓配未包含在已确认站点上传及费用合同的适用范围")

    profit_check = online.profit_check
    if profit_check is None:
        return finish("unverified_online_profit", "缺少库存分流、部门费率及完整在线利润公式")
    if problem := profit_check.problem(now, snapshot.business_date):
        return finish("unverified_online_profit", problem)
    if model.revenue_factor <= 0 or model.fixed_cost < 0:
        return finish("unverified_fee_model", "正常一次毛利模型不能求得保本价")

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
            elif row.basis == "order" and (
                not row.allocation_reliable or not row.order_id.strip() or _order_scope(row) in multi_line_orders
            ):
                # 整单金额仅接受有可靠证据且可归属到单个明细的订单。
                # 若同批已见多条明细，不按总件数分摊，也不让误标覆盖证据。
                excluded_rows[row.row_id] = "unreliable_order_allocation"
            else:
                magnitude = MoneyRatio(row.loss_amount.copy_abs())
                valid_losses[row.row_id] = magnitude if row.basis == "unit" else magnitude / MoneyRatio(row.quantity)
        if not valid_losses:
            return finish("no_valid_loss", "没有可可靠归一到单件的负亏损明细")
        trace["candidate_unit_losses"] = {row_id: str(value.decimal()) for row_id, value in valid_losses.items()}
        selected = min(valid_losses, key=lambda row_id: (valid_losses[row_id], row_id))
        unit_ratio = valid_losses[selected]

        # 原币/CNY报价用站点汇率/亏损币汇率；CNY/原币报价用亏损币汇率/站点汇率。
        # ERP当前配置直接传原报价及单位，不把50位展示倒数回读为业务输入。
        # 保留精确 Decimal 比值直到档位与毛利判定，避免1/3等近似值少算一档。
        rate_numerator, rate_denominator = (
            (from_rate, to_rate) if snapshot.fx_rate_unit == 'cny_per_currency' else (to_rate, from_rate)
        )
        station_ratio = (unit_ratio if loss_currency == station_currency
                         else unit_ratio * MoneyRatio(rate_numerator, rate_denominator))
        # 11/12会议后段：先直接补亏，再只复算库存选定仓配的完整在线利润。
        # 对已核实仿射公式一次求保本下限，等价于继续提价到不亏；不设1%目标。
        initial_target = MoneyRatio(current) + station_ratio
        online_floor = profit_check.break_even()
        normal_floor = MoneyRatio(model.fixed_cost, model.fixed_cost_divisor) / MoneyRatio(model.revenue_factor)
        target_ratio = max(initial_target, online_floor, normal_floor)
        discount = MoneyRatio(current, retail).decimal()
        # 含 VAT 上传价 = T×R/P；按精确商余数舍入，可支持0.025档位及半档边界。
        raw_ratio = target_ratio * MoneyRatio(retail) / MoneyRatio(current)
        upload_price = raw_ratio.round_to_step(contract.price_step, contract.rounding)

        # 原合同舍入不得把已达到的保本价再次舍入到负利润；最低可用上传档位向上取整。
        floor_upload = (max(online_floor, normal_floor) * MoneyRatio(retail) / MoneyRatio(current)).round_to_step(
            contract.price_step, "ROUND_UP")
        upload_price = max(upload_price, floor_upload)
        rounded_ratio = MoneyRatio(upload_price) * MoneyRatio(current) / MoneyRatio(retail)
        error_ratio = (rounded_ratio - target_ratio).absolute()
        tolerance = allowed_error(key.site)
        online_target_profit = profit_check.model.profit_exact(target_ratio)
        online_rounded_profit = profit_check.model.profit_exact(rounded_ratio)
        target_profit_ratio = model.profit_exact(target_ratio)
        rounded_profit_ratio = model.profit_exact(rounded_ratio)
        # 下面的有限小数仅用于持久化/展示；分支仍使用精确比值，不给负毛利加容差。
        unit_loss, station_loss = unit_ratio.decimal(), station_ratio.decimal()
        target_actual, raw_upload = target_ratio.decimal(), raw_ratio.decimal()
        rounded_actual, error = rounded_ratio.decimal(), error_ratio.decimal()
        target_profit, rounded_profit = target_profit_ratio.decimal(), rounded_profit_ratio.decimal()
    except DecimalException:
        return finish("invalid_calculation", "价格、汇率或费用模型超出可计算范围")

    trace.update({
        "loss_fx_rate": str(from_rate),
        "station_fx_rate": str(to_rate),
        "raw_upload_price": str(raw_upload),
        "exact_target_actual": {"numerator": str(target_ratio.numerator), "denominator": str(target_ratio.denominator)},
        "rounded_actual": str(rounded_actual),
        "actual_price_error": str(error),
        "allowed_error": str(tolerance),
        "target_profit": str(target_profit),
        "online_profit": {
            "mode": profit_check.mode, "department": profit_check.department,
            "overseas_stock": str(profit_check.overseas_stock),
            "inventory_observed_at": profit_check.inventory_observed_at.isoformat(),
            "source_ref": profit_check.source_ref, "model": profit_check.model.model_dump(mode="json"),
            "initial_target": str(initial_target.decimal()), "break_even": str(online_floor.decimal()),
            "adjusted": target_ratio > initial_target, "minimum_margin": "0",
            "target_margin": str((online_target_profit / target_ratio).decimal()),
            "rounded_margin": (str((online_rounded_profit / rounded_ratio).decimal())
                               if rounded_ratio.numerator > 0 else None),
        },
    })
    prices = {
        "selected_row_id": selected, "unit_loss": unit_loss, "loss_currency": loss_currency,
        "station_loss": station_loss, "current_actual": current, "discount": discount,
        "target_actual": target_actual, "upload_price": upload_price,
        "predicted_profit": rounded_profit, "upload_column": contract.upload_column,
    }
    if upload_price <= 0 or error_ratio > MoneyRatio(tolerance):
        return finish("rounding_error", "站点价格档位导致实际价无效或超出误差容限", **prices)
    if online_target_profit.numerator < 0 or online_rounded_profit.numerator < 0:
        return finish("online_profit_shortfall", "目标价或档位后在线利润率仍为负", **prices)
    if target_profit_ratio.numerator < 0 or rounded_profit_ratio.numerator < 0:
        return finish("fee_shortfall", "直接补亏后的目标价或舍入后实际价仍亏损", **prices)
    return finish("ready", "补亏价格通过站点精度、正常一次毛利及库存对应在线利润率核验", ready=True, **prices)
