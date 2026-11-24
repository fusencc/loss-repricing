from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException, localcontext
from itertools import chain

from .money import MoneyRatio, PRICE_CONTEXT
from .price_reference import reference_upper_price, describe_price_limit

from .types import (
    CandidateSourceIssue, Decision, DecisionStatus, OnlineState, Site, Snapshot,
    SourceIssue, SourceLine, VariantKey, ONLINE_PROFIT_MINIMUM_MARGIN,
)


# 业务时间统一为当前中国标准时间，Windows 环境也可直接使用此固定时差。
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
REQUIRED_SOURCES = frozenset({"loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"})
# 依据：已确认设计“数据与算价说明”；完整变量/单位见 docs/calculation-and-sources.md。
# ERP 修正一次毛利直接作为亏损来源；近72小时保护用官方计数，费用仅在完整在线利润校验。
# 规则版本不是公司费率或接口验收标志，固定表另存内容版本。
FORMULA_VERSION = "loss-erp-corrected-margin-reference-priority-20261123-v14"
# 仅供完整旧日快照的跨午夜评估；真实提交依然重取当日数据。
MIDNIGHT_EVALUATION_WINDOW = timedelta(minutes=15)
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


def fx_ratio(snapshot: Snapshot, loss_currency: str, station_currency: str) -> MoneyRatio | None:
    """按快照的原始报价换算亏损币到站点币。同币种也须有有效报价。"""
    from_rate = snapshot.fx_rates.get(loss_currency)
    to_rate = snapshot.fx_rates.get(station_currency)
    if from_rate is None or from_rate <= 0 or to_rate is None or to_rate <= 0:
        return None
    if loss_currency == station_currency:
        return MoneyRatio(Decimal(1))
    # 每单位原币折多少 CNY 时取亏损币报价 / 站点币报价。反向报价交换分子分母。
    if snapshot.fx_rate_unit == 'cny_per_currency':
        return MoneyRatio(from_rate, to_rate)
    return MoneyRatio(to_rate, from_rate)


def _valid_evaluation_observation(observed_at: datetime, now: datetime, business_date: date) -> bool:
    return (observed_at.astimezone(BEIJING).date() == business_date and observed_at <= now
            and (business_date == now.astimezone(BEIJING).date()
                 or now - observed_at <= MIDNIGHT_EVALUATION_WINDOW))


def conflicting_main_sku_scopes(
    online: Iterable[OnlineState], now: datetime, *, business_date: date | None = None,
) -> set[tuple[str, str, Site, str]]:
    """同一上传目标被有效记录指向多个主 SKU 才构成归属冲突。"""
    business_date = business_date or now.astimezone(BEIJING).date()
    scopes_by_target = defaultdict(set)
    for state in online:
        if not state.main_sku.strip() or not _valid_evaluation_observation(state.observed_at, now, business_date):
            continue
        scope = state.key.sales_scope(state.main_sku)
        scopes_by_target[state.key.target_identity()].add(scope)
    conflicted = set()
    for scopes in scopes_by_target.values():
        if len(scopes) > 1:
            # 同一上传对象被关联到不同主 SKU，相关身份定义均需核对。
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
    return _evaluate(snapshot, now, snapshot_problem(snapshot, now))


def evaluate_erp_readonly_preview(snapshot: Snapshot, now: datetime) -> list[Decision]:
    """实时 ERP 预览的订单已逐批核对；只免除四名单齐备条件，不改变汇率和日期检查。"""
    return _evaluate(snapshot, now, _snapshot_date_fx_problem(snapshot, now))


def _evaluate(snapshot: Snapshot, now: datetime, batch_problem) -> list[Decision]:
    conflicted_sales = conflicting_main_sku_scopes(snapshot.online, now, business_date=snapshot.business_date)
    candidate_groups: dict[str, list[SourceLine | CandidateSourceIssue]] = defaultdict(list)
    online_groups: dict[str, list[OnlineState]] = defaultdict(list)
    order_lines = defaultdict(set)
    detail_mappings = defaultdict(set)
    issues_by_identity = defaultdict(list)
    first_issue_by_target = {}
    first_issue_by_detail = {}
    candidate_issues = []
    for index, issue in enumerate(snapshot.source_issues):
        issues_by_identity[issue.key.identity()].append(index)
        first_issue_by_target.setdefault(issue.key.target_identity(), index)
        if isinstance(issue, CandidateSourceIssue):
            candidate_issues.append(issue)
            first_issue_by_detail.setdefault((*_order_scope(issue), issue.row_id), index)
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
        # 字段异常影响同一在线目标或订单明细，不因共用主 SKU 扩大阻断范围。
        issue_links = dict.fromkeys(issues_by_identity.get(identity, ()))
        detail_issue = next((
            index for row in group
            if (index := first_issue_by_detail.get((*_order_scope(row), row.row_id))) is not None
        ), None)
        for relation, index in (
            ("same_target", first_issue_by_target.get(target)),
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
                problem = ("source_record_invalid", "该子 SKU 的来源字段异常，需核对本商品或关联订单明细")
            elif conflicted_sales and any(
                row.key.sales_scope(row.main_sku) in conflicted_sales for row in chain(rows, states)
            ):
                problem = ("conflicting_main_sku_sales", "共享主 SKU 的在线归属存在冲突")
            results.append(_decide(snapshot, now, problem, key, rows, states, multi_line_orders, issues))
    return results


def snapshot_problem(snapshot: Snapshot, now: datetime, required_business_date: date | None = None) -> tuple[str, str] | None:
    # 待处理来源也必须完整，避免因漏源选了偏大的单件亏损。
    sources = snapshot.complete_sources
    if len(sources) != len(REQUIRED_SOURCES) or set(sources) != REQUIRED_SOURCES:
        return "incomplete_sources", "ERP 四份候选来源尚未完整齐备"
    return _snapshot_date_fx_problem(snapshot, now, required_business_date)


def _snapshot_date_fx_problem(snapshot: Snapshot, now: datetime, required_business_date: date | None = None):
    today = now.astimezone(BEIJING).date()
    business_date = snapshot.business_date
    # 只容许完整的上一日快照在午夜后短时评估；采集日、业务日与汇率使用日仍须同属一批。
    if (required_business_date is not None and business_date != required_business_date
            or business_date not in (today, today - timedelta(days=1))
            or not _valid_evaluation_observation(snapshot.as_of, now, business_date)):
        return "stale_snapshot", "快照采集时间无效或超过北京时间跨日评估的15分钟窗口"
    # 两种报价的 CNY 基准都须为 1；不能拿旧汇率拼接新一天的快照。
    if snapshot.fx_date != business_date or snapshot.fx_rates.get("CNY") != Decimal("1"):
        return "invalid_fx", "缺少与快照业务日一致、以 CNY 为基准的有效汇率"
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
        "warnings": list(dict.fromkeys(warning for state in states if state.profit_check
                                       for warning in state.profit_check.warnings())),
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

    # 2026-11-20：历史未知、不可售仅告警；计算依据无效本轮不调，不伪造价格。
    def warn(code, message):
        trace.setdefault("warning_codes", []).append(code)
        trace["warnings"].append(message)

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
    if any(state.model_dump(exclude={'sku_price_reference'})
           != online.model_dump(exclude={'sku_price_reference'}) for state in states[1:]):
        return finish("conflicting_mapping", "同一在线子 SKU 存在多条冲突的在线快照")
    # 参考观察不构成现价冲突；有参考时在算价处执行上限，缺失不阻断。
    references = [state.sku_price_reference for state in states
                  if state.sku_price_reference is not None and state.sku_price_reference.currency == online.currency]
    # 重复现态不因参考差异挂起，也不能以行序或异币种参考绕过已存在的上限。
    price_reference = min(references, key=lambda ref: reference_upper_price(ref, online.currency), default=None)
    # 核心现态一致的重复观察合并为一条执行依据；原始观察仍在批次快照中留证。
    # 保存选中参考，保证后续回读只有一个利润模型来源且不会因重复行永久挂起。
    online = online.model_copy(update={'sku_price_reference': price_reference})
    trace['online_rows'] = [online.model_dump(mode='json')]
    if price_reference is not None:
        trace['sku_price_reference'] = price_reference.model_dump(mode='json')
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
    # 在线售价、完整在线利润模型和上传价采用站点币。ERP 修正一次毛利原币独立校验并换汇。
    station_currency = SITE_CURRENCIES[key.site]
    trace["station_currency"] = station_currency
    if online.currency != station_currency:
        return finish("invalid_station_currency", f"在线币种与 {key.site} 站点币 {station_currency} 不一致")
    if not _valid_evaluation_observation(online.observed_at, now, snapshot.business_date):
        return finish("stale_online", "在线价格与保护数据不属于快照业务日或已过跨日评估窗口")
    if online.last_changed_at is not None and online.last_changed_at > online.observed_at:
        return finish("stale_online", "价格变更时间晚于该价格的观察时间")

    # 未完成提交持续锁定；真实近期调价保护使用官方日志计数。
    if online.pending_submission:
        return finish("pending_submission", "已有未完成核验的提交")
    if not online.history_known:
        warn("unknown_history", "近72小时调价历史无法确认，仅告警")
    if online.last_increase_at is not None and online.last_increase_at > now:
        warn("unknown_history", "上次确认生效时间晚于评估时间，历史无法确认，仅告警")
    if online.protected:
        return finish("manual_protection", "命中人工保护名单", status="本轮不调")
    if online.recent_price_log is not None:
        history = online.recent_price_log
        if (not history.source_id or history.window_end-history.window_start != timedelta(hours=72)
                or history.window_end > now
                or history.window_end.astimezone(BEIJING).date()!=snapshot.business_date):
            warn("unknown_history", "开放调价日志查询窗口无效或不属于本业务日，仅告警")
        elif history.count > 0:
            return finish("recent_price_change", "近72小时存在调价日志，本轮不调", status="本轮不调")
    elif online.last_increase_at is not None and online.last_increase_at <= now:
        elapsed = now.astimezone(timezone.utc) - online.last_increase_at.astimezone(timezone.utc)
        if elapsed < timedelta(hours=72):
            return finish("recent_increase", "距离上次确认生效不足 72 小时", status="本轮不调")

    # 2026-11-20用户暂停活动限制：参加活动不再拦截算价；营销价、折扣及有效价核验保持不变。
    if not online.sellable:
        warn("not_sellable", "子 SKU 当前不可售，仅告警")

    if (
        contract is None
        or contract.site != key.site
        or not contract.verified
        or contract.price_step <= 0
        or not contract.upload_column.strip()
        or not contract.version.strip()
    ):
        warn("unverified_site_contract", "缺少经确认的站点上传字段、价格档位或合同版本")
        return finish("unverified_site_contract", "缺少经确认的站点上传字段、价格档位或合同版本", status="本轮不调")
    if key.warehouse not in contract.warehouses:
        warn("unsupported_warehouse", "当前仓配未包含在已确认站点上传及费用合同的适用范围")
        return finish("unsupported_warehouse", "当前仓配未包含在已确认站点上传及费用合同的适用范围", status="本轮不调")

    profit_check = online.profit_check
    if profit_check is None:
        warn("unverified_online_profit", "缺少库存分流、部门费率及完整在线利润公式")
        return finish("unverified_online_profit", "缺少库存分流、部门费率及完整在线利润公式", status="本轮不调")
    upper = reference_upper_price(price_reference, station_currency)
    minimum_margin_required = upper is None
    if issue := profit_check.validation_issue(now, snapshot.business_date,
                                              minimum_margin_required=minimum_margin_required):
        warn("unverified_online_profit", issue.value)
        return finish("unverified_online_profit", issue.value, status="本轮不调")

    conversion = fx_ratio(snapshot, loss_currency, station_currency)
    if conversion is None:
        return finish("invalid_fx", "亏损原币或站点币缺少正数汇率")

    observed_prices = online.actual_price_and_discount()
    if observed_prices is None:
        return finish("invalid_current_price", "当前含 VAT 零售价或实际营销价无效")
    current, marketing_discount_ratio = observed_prices

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

        station_ratio = unit_ratio * conversion
        # 11/12会议后段：先直接补亏，再只复算库存选定仓配的完整在线利润。
        # 无参考时要求1%；有参考时优先上限，利润不足或需降价均不再拦截。
        initial_target = MoneyRatio(current) + station_ratio
        # 单独保存补亏候选价的站点币利润与利润率，供核对利润校正和参考限价前的状态。
        initial_result = profit_check.model.evaluate(initial_target)
        minimum_actual = profit_check.minimum_actual_price()
        unlimited_target = max(initial_target, minimum_actual) if minimum_actual is not None else None
        target_ratio = unlimited_target
        # 不可达的1%价格不伪造校正额；上限压减单列，不记成负校正额。
        online_profit_correction = unlimited_target - initial_target if unlimited_target is not None else None
        maximum_upload = None
        if upper is not None:
            # 用户确认参考为硬上限，先向下取可上传档位，防止进档后突破营销均价。
            maximum_upload = (upper / marketing_discount_ratio).round_to_step(
                contract.price_step, 'ROUND_DOWN')
            maximum_actual = MoneyRatio(maximum_upload) * marketing_discount_ratio
            target_ratio = (min(unlimited_target, maximum_actual)
                            if unlimited_target is not None else maximum_actual)
            trace['price_limit'] = describe_price_limit(upper, unlimited_target, target_ratio)
        discount = marketing_discount_ratio.decimal()
        # 含 VAT 上传价 = T×R/P；按精确商余数舍入，可支持0.025档位及半档边界。
        raw_ratio = target_ratio / marketing_discount_ratio
        upload_price = raw_ratio.round_to_step(contract.price_step, contract.rounding)

        # 能达到1%时先保留利润档位；参考上限始终优先，允许压到利润下限以下。
        if minimum_actual is not None:
            floor_upload = (minimum_actual / marketing_discount_ratio).round_to_step(
                contract.price_step, "ROUND_UP")
            upload_price = max(upload_price, floor_upload)
        if maximum_upload is not None:
            upload_price = min(upload_price, maximum_upload)
        rounded_ratio = MoneyRatio(upload_price) * marketing_discount_ratio
        error_ratio = (rounded_ratio - target_ratio).absolute()
        tolerance = allowed_error(key.site)
        target_result = profit_check.model.evaluate(target_ratio)
        rounded_result = profit_check.model.evaluate(rounded_ratio)
        # 下面的有限小数仅用于持久化/展示；分支仍使用精确比值，不给负毛利加容差。
        unit_loss, station_loss = unit_ratio.decimal(), station_ratio.decimal()
        target_actual, raw_upload = target_ratio.decimal(), raw_ratio.decimal()
        rounded_actual, error = rounded_ratio.decimal(), error_ratio.decimal()
        target_profit, rounded_profit = target_result.profit.decimal(), rounded_result.profit.decimal()
    except DecimalException:
        return finish("invalid_calculation", "价格、汇率或费用模型超出可计算范围")

    trace.update({
        "loss_fx_rate": str(snapshot.fx_rates[loss_currency]),
        "station_fx_rate": str(snapshot.fx_rates[station_currency]),
        "raw_upload_price": str(raw_upload),
        "exact_target_actual": {"numerator": str(target_ratio.numerator), "denominator": str(target_ratio.denominator)},
        "rounded_actual": str(rounded_actual),
        "actual_price_error": str(error),
        "allowed_error": str(tolerance),
        "target_profit": str(target_profit),
        "online_profit": {
            "mode": profit_check.mode, "department": profit_check.department,
            "overseas_stock": None if profit_check.overseas_stock is None else str(profit_check.overseas_stock),
            "inventory_observed_at": profit_check.inventory_observed_at.isoformat(),
            "source_ref": profit_check.source_ref, "model": profit_check.model.model_dump(mode="json"),
            "initial_target": str(initial_target.decimal()),
            "initial_profit": str(initial_result.profit.decimal()),
            "initial_margin": str(initial_result.margin.decimal()) if initial_result.margin is not None else None,
            "break_even": str(minimum_actual.decimal()) if minimum_actual is not None else None,
            "correction_amount": str(online_profit_correction.decimal()) if online_profit_correction is not None else None,
            "adjusted": unlimited_target is not None and unlimited_target > initial_target,
            "minimum_margin": str(ONLINE_PROFIT_MINIMUM_MARGIN),
            "minimum_margin_required": minimum_margin_required,
            "target_margin": str(target_result.margin.decimal()) if target_result.margin is not None else None,
            "rounded_margin": str(rounded_result.margin.decimal()) if rounded_result.margin is not None else None,
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
    if upper is not None and rounded_ratio > upper:
        return finish('reference_price_exceeded', '进档后营销价超过参考上限，本轮不调', status='本轮不调', **prices)
    if minimum_margin_required and (not target_result.meets_minimum_margin or not rounded_result.meets_minimum_margin):
        return finish("online_profit_shortfall", "目标价或档位后在线利润率未达到1%", **prices)
    reason = ("已按参考价优先确定价格，预计在线利润已记录" if upper is not None else
              "补亏价格通过站点精度、库存对应完整在线利润率核验")
    return finish("ready", reason, ready=True, **prices)
