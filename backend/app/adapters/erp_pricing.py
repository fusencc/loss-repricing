"""把明确选择的 ERP 批量文件和独立证据组装成只读补亏评估输入。"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
import json
import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from app.adapters.erp_bulk_capture import ERP_DEPARTMENTS, SOURCES, STATUS_LABELS, finance_batch_scope
from app.adapters.normalization import identity_text, sku_text, source_date, source_time
from app.domain.engine import BEIJING, conflicting_main_sku_scopes
from app.domain.types import OnlineProfitCheck, FeeModel, OnlineState, SiteContract, Snapshot, Source, SourceLine, VariantKey

# 仅消费今天已核对的批量合同。逐单诊断与规范 Snapshot 各有独立入口。
CAPTURE_VERSION = "erp-web-bulk-four-20261109"
EVIDENCE_VERSION = "erp-pricing-evidence-v2"


def decimal_text(value):
    # JSON 使用明文十进制字符串，禁止通过 float 或指数记法重建业务金额。
    if not isinstance(value, str) or len(value) > 80 or not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("金额、数量及汇率须为十进制字符串")
    return Decimal(value)


Identifier = Annotated[StrictStr, Field(max_length=256), AfterValidator(identity_text)]
SkuIdentifier = Annotated[StrictStr, Field(max_length=256), AfterValidator(sku_text)]
Version = Annotated[StrictStr, Field(pattern=r"^[\w.-]{1,128}$")]
Currency = Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
DecimalText = Annotated[Decimal, BeforeValidator(decimal_text)]
SourceTime = Annotated[AwareDatetime, BeforeValidator(source_time)]
SourceDate = Annotated[date, BeforeValidator(source_date)]


class FinanceFields(BaseModel):
    order_id: Identifier = Field(alias="订单编号")
    order_status: StrictStr | None = Field(alias="订单状态")
    platform: StrictStr = Field(alias="平台")
    bloc: StrictStr = Field(alias="集团")
    shop: StrictStr | None = Field(default=None, alias="店铺")
    salesperson: StrictStr | None = Field(default=None, alias="销售员")
    department: StrictStr | None = Field(default=None, alias="部门")
    product_hint: Any = Field(default=None, alias="item ID")
    # ERP 财务表按订单导出的修正一次毛利，原样展示十进制文本和空值。
    # 只有完整同SKU明细、总件数与RMB来源合同另经核对后，才可按整单粒度准备输入。
    corrected_first_margin: Any = Field(default=None, alias="修正一次毛利")


class FinanceRow(BaseModel):
    orderid: Identifier
    current_status_matches_source: StrictBool
    fields: FinanceFields


class ExportPoll(BaseModel):
    queried_at: SourceTime
    status: StrictStr | None


class ExportScope(BaseModel):
    requested: StrictInt
    downloaded_rows: StrictInt = Field(ge=0)
    downloaded_unique: StrictInt
    missing: list[Identifier]
    unexpected: list[Identifier]
    duplicate_rows: StrictInt


class ExportBatch(BaseModel):
    number: StrictInt | None = None
    scope_check: ExportScope | None = None
    order_ids: list[Identifier] = Field(default_factory=list)
    status: StrictStr | None = None
    polls: list[ExportPoll]
    downloaded_at: SourceTime | None = None


class CaptureGroup(BaseModel):
    source: Source
    status: StrictInt
    filters: dict
    list_complete: StrictBool
    stage: StrictStr
    error: StrictStr | None
    total: StrictInt | None
    candidate_order_ids: list[Identifier]
    rows: list[FinanceRow]
    batches: list[ExportBatch]
    missing_order_ids: list[Identifier] = Field(default_factory=list)
    status_discrepancies: list[dict] = Field(default_factory=list)
    failed_pages: list = Field(default_factory=list)

    def scope_bloc(self, department=None, salesperson=None):
        kind, expected_status = SOURCES[self.source]
        if self.status != expected_status or self.filters.get("status") != expected_status:
            return None
        # 只接受同一部门的四组范围。无部门的采集必须没有部门选择字段。
        department_key = "department" if kind == "loss" else "departments"
        extra_scope = {department_key} if department is not None else set()
        if department is not None and (
            department not in ERP_DEPARTMENTS or self.filters.get(department_key) != department
        ):
            return None
        # 同一销售员在亏损池用姓名，在禁止发货列表用 ERP 人员 ID。
        # 每组按实际请求字段核对，缺失条件的集团名单不能计入个人范围。
        if salesperson is not None:
            if not salesperson.name or salesperson.name != salesperson.name.strip():
                return None
            key, value = ("username", salesperson.name) if kind == "loss" else ("shopUsernameid", salesperson.id)
            if self.filters.get(key) != value:
                return None
            extra_scope.add(key)
        filter_keys = set(self.filters) - extra_scope
        if kind == "loss":
            if (
                filter_keys != {"platform", "bloc", "status", "financecheck"}
                or str(self.filters.get("platform", "")).casefold() != "tiktok"
                or self.filters.get("financecheck") != "3"
            ):
                return None
            bloc = self.filters.get("bloc")
        else:
            if (
                filter_keys != {"ptid", "blocs", "status", "jzfh"}
                or not self.filters.get("ptid") or self.filters.get("jzfh") != "1"
            ):
                return None
            bloc = self.filters.get("blocs")
        return bloc if isinstance(bloc, str) and bloc.strip() else None

    @staticmethod
    def row_in_scope(row, bloc, department, salesperson):
        fields = row.fields
        return (bloc is not None and fields.order_id == row.orderid and fields.bloc == bloc
                and fields.platform.casefold() == 'tiktok'
                and (salesperson is None or fields.salesperson == salesperson.name)
                and (department is None or (fields.department or '').casefold() == department.casefold()))

    def finance_coverage(self, bloc, department=None, salesperson=None):
        ids = set(self.candidate_order_ids)
        row_ids = {row.orderid for row in self.rows}
        list_known = self.list_complete and self.total == len(ids) == len(self.candidate_order_ids)
        rows_in_scope = (
            bloc is not None and row_ids <= ids
            and all(row.fields.order_id == row.orderid and row.fields.bloc == bloc
                    and row.fields.platform.casefold() == "tiktok"
                    and (salesperson is None or row.fields.salesperson == salesperson.name)
                    and (department is None or (row.fields.department or "").casefold() == department.casefold())
                    for row in self.rows)
        )
        if not rows_in_scope:
            return False, False
        # 2026-11-25 用户授权“抓到多少处理多少”：名单后页或财务后批失败，
        # 不否定此前已绑定任务、成功下载且逐批核对的财务行。完整性仍如实报告，
        # 未返回订单不当作零亏损；只使用已观察样本，不声称取得全量最小亏损。
        partial_capture = self.stage not in ("complete", "not_attempted") and (
            self.error is not None or bool(self.failed_pages)
        )
        if partial_capture and self.rows and self._batches_cover_orders(ids, allow_partial=True):
            return False, True
        if not list_known or not self._batches_cover_orders(ids):
            return False, False
        # 最终导出中已知订单的缺行或重复可按目标挂起。未完成导出无法证明这类缺口已定位。
        has_duplicates = len(row_ids) != len(self.rows)
        complete = row_ids == ids and not has_duplicates and self.stage == "complete" and self.error is None
        known_issue = (
            (self.stage == "complete_with_missing" and row_ids < ids and not has_duplicates)
            or (self.stage == "finance_conflict" and has_duplicates)
        )
        return complete, known_issue

    def _batches_cover_orders(self, candidate_ids, *, allow_partial=False):
        # 采集按子批依次追加财务行。按记录行数分段核对，不能用整组订单并集证明子批无串单。
        requested = set()
        offset = 0
        for number, batch in enumerate(self.batches, 1):
            ids = set(batch.order_ids)
            if (
                batch.number != number
                or not ids or len(ids) != len(batch.order_ids) or ids & requested
                or not ids <= candidate_ids
            ):
                return False
            requested.update(ids)
            if allow_partial and batch.status in ("not_attempted", "submitting", "polling", "failed"):
                # 此批无可信财务行；不消耗 rows，后续成功子批仍独立核对。
                continue
            if (batch.scope_check is None or not batch.polls
                    or batch.polls[-1].status != "success"):
                return False
            end = offset + batch.scope_check.downloaded_rows
            scope = finance_batch_scope(batch.order_ids, [row.orderid for row in self.rows[offset:end]])
            expected_status = "partial" if scope["missing"] or scope["duplicate_rows"] else "complete"
            if scope["unexpected"] or scope != batch.scope_check.model_dump() or batch.status != expected_status:
                return False
            offset = end
        return (allow_partial or requested == candidate_ids) and offset == len(self.rows)


class CaptureSalesperson(BaseModel):
    name: StrictStr
    id: Identifier


class BulkCapture(BaseModel):
    acquisition_mode: Literal["bulk_download"]
    source_kind: Literal["erp_web_backend"]
    source_version: Literal[CAPTURE_VERSION]
    capture_id: Identifier
    queried_at: SourceTime
    synthetic: StrictBool = False
    sample_only: StrictBool
    capture_complete: StrictBool
    groups: list[CaptureGroup]
    salesperson: CaptureSalesperson | None = None
    department: StrictStr | None = None


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MappedKey(VariantKey):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["TikTok"]
    shop_id: Identifier
    product_id: Identifier
    item_id: Identifier
    sku: SkuIdentifier
    warehouse: Identifier


class SkuMapping(EvidenceModel):
    erp_shop: Identifier
    key: MappedKey
    main_sku: Identifier
    ref: Identifier


class LineLoss(EvidenceModel):
    quantity: DecimalText
    amount: DecimalText
    currency: Currency
    basis: Literal["unit", "line", "order"]
    allocation_reliable: StrictBool
    fee_scope: Identifier
    abnormal: StrictBool
    ref: Identifier


class DetailEvidence(EvidenceModel):
    detail_id: Identifier
    detail_ref: Identifier
    mapping: SkuMapping | None
    loss: LineLoss | None


class OrderEvidence(EvidenceModel):
    order_id: Identifier
    details_complete: StrictBool
    lines: list[DetailEvidence]


class ReviewedFeeModel(FeeModel):
    model_config = ConfigDict(extra="forbid")

    revenue_factor: DecimalText
    fixed_cost: DecimalText
    fixed_cost_divisor: DecimalText = Field(default=Decimal(1), gt=0)
    version: Version
    verified: StrictBool


class ReviewedOnlineProfitCheck(OnlineProfitCheck):
    model_config = ConfigDict(extra="forbid")
    overseas_stock: DecimalText | None = None
    department: Identifier
    source_ref: Identifier
    inventory_observed_at: SourceTime
    model: ReviewedFeeModel


class ReviewedOnlineState(OnlineState):
    model_config = ConfigDict(extra="forbid")

    key: MappedKey
    main_sku: Identifier
    currency: Currency
    retail_price: DecimalText
    marketing_price: DecimalText | None
    fee_scope: Identifier
    profit_check: ReviewedOnlineProfitCheck | None = None
    observed_at: SourceTime
    history_known: StrictBool
    last_increase_at: SourceTime | None
    pending_submission: StrictBool
    protected: StrictBool
    activity_locked: StrictBool
    sellable: StrictBool


class OnlineEvidence(EvidenceModel):
    ref: Identifier
    state: ReviewedOnlineState


class ReviewedSiteContract(SiteContract):
    model_config = ConfigDict(extra="forbid")

    verified: StrictBool
    price_step: DecimalText
    upload_column: Identifier
    version: Version
    warehouses: list[Identifier]


class ContractEvidence(EvidenceModel):
    ref: Identifier
    contract: ReviewedSiteContract


class FxEvidence(EvidenceModel):
    date: SourceDate
    rates: dict[Currency, DecimalText]
    version: Version
    ref: Identifier


class CurrentFxEvidence(EvidenceModel):
    date: SourceDate
    version: Version
    ref: Identifier
    queried_at: SourceTime
    synthetic: StrictBool
    source_effective_date: None
    date_policy: Literal['current_config_on_usage_day']
    source_rates: dict[Currency, DecimalText]
    display_units_per_cny: dict[Currency, DecimalText]
    source_observation: dict = Field(default_factory=dict)


class EvidenceVersions(EvidenceModel):
    details: Version
    mapping: Version
    online: Version


class PricingEvidence(EvidenceModel):
    schema_version: Literal[EVIDENCE_VERSION]
    capture_id: Identifier
    synthetic: StrictBool
    evaluated_at: SourceTime
    source_versions: EvidenceVersions
    orders: list[OrderEvidence]
    online: list[OnlineEvidence]
    fx: FxEvidence | None
    contracts: list[ContractEvidence]


class LiveOnlinePreparation(EvidenceModel):
    schema_version: Literal['erp-live-online-preparation-v2']
    version: Literal['erp-live-online-v2']
    evaluated_at: SourceTime
    erp_capture_id: Identifier
    synthetic: StrictBool
    sample_only: StrictBool
    pricing_ready: Literal[False]
    observed_at: SourceTime
    online: list[OnlineEvidence]
    contracts: list[ContractEvidence]
    issues: list[dict]
    references: dict


class SameSkuOrderProof(EvidenceModel):
    record_kind: Literal['same_sku_order']
    order_record_id: Identifier
    native_refs: list[Identifier] = Field(min_length=1)
    quantity: DecimalText
    erp_sku: SkuIdentifier
    finance_refs: list[dict] = Field(min_length=1)
    currency: Literal['CNY']
    currency_contract: Literal['erp-order-finance-rmb-20261111']


class LiveShopScope(EvidenceModel):
    shop_id: Identifier
    erp_shop: Identifier
    ref: Identifier


class LiveOrderPreparation(EvidenceModel):
    schema_version: Literal['erp-order-loss-preparation-v1']
    version: Literal['erp-same-sku-order-loss-v1']
    erp_capture_id: Identifier
    synthetic: StrictBool
    sample_only: StrictBool
    pricing_ready: Literal[False]
    observed_at: SourceTime
    orders: list[OrderEvidence]
    issues: list[dict]
    proofs: dict[str, SameSkuOrderProof]
    shop_scopes: dict[Identifier, LiveShopScope]
    references: dict
    summary: dict

    @model_validator(mode='after')
    def validate_order_records(self):
        ids = [order.order_id for order in self.orders]
        if len(set(ids)) != len(ids) or set(ids) != self.proofs.keys():
            raise ValueError('订单级金额记录重复或缺少归属证明')
        if self.references.get('currency_contract') != 'erp-order-finance-rmb-20261111':
            raise ValueError('订单级金额缺少已核对的币种合同')
        seen_refs = set()
        for order in self.orders:
            if not order.details_complete or len(order.lines) != 1:
                raise ValueError('同SKU整单金额须是一条完整金额记录')
            line, proof = order.lines[0], self.proofs[order.order_id]
            if (line.detail_id != order.order_id or proof.order_record_id != order.order_id
                    or line.loss is None or line.loss.basis != 'order' or line.loss.currency != 'CNY'
                    or not line.loss.allocation_reliable or line.loss.abnormal
                    or line.loss.fee_scope != 'erp-corrected-first-margin-v1'
                    or proof.quantity <= 0 or proof.quantity != line.loss.quantity
                    or len(set(proof.native_refs)) != len(proof.native_refs)
                    or seen_refs.intersection(proof.native_refs)):
                raise ValueError('同SKU整单金额与数量、身份或物理行证明不一致')
            seen_refs.update(proof.native_refs)
        return self


ISSUE_REASONS = {
    "missing_order_details": "缺少该订单的商品明细证据",
    "incomplete_order_details": "商品明细未声明取全、为空或明细标识重复",
    "missing_order_finance": "至少一个候选来源缺少对应订单财务行",
    "duplicate_finance_rows": "同一候选来源返回了该订单的重复财务行，须核实该商品",
    "order_status_conflict": "订单在来源之间状态冲突，或财务导出时已离开该候选状态",
    "missing_order_shop": "已返回的订单财务行缺少店铺，无法核对在线映射",
    "order_shop_conflict": "同一订单在不同来源中对应的店铺不一致",
    "mapping_shop_mismatch": "补充映射的 ERP 店铺与采集财务行不一致",
    "conflicting_main_sku_sales": "同店同站同一主 SKU 的七日完整销量相互矛盾",
    "missing_online_mapping": "缺少到具体在线子 SKU 的可靠映射",
    "missing_line_loss": "缺少可归属到该明细的亏损金额、数量或币种证据",
    "unreliable_order_loss": "整单金额需要可靠同SKU归属证明，不能向多个独立明细重复使用",
    "target_held": "同一在线商品还有未解决的订单证据，暂不能确定最小有效亏损",
}


@dataclass
class Preparation:
    report: dict
    snapshot: Snapshot | None
    evaluated_at: datetime | None


def prepare_pricing(capture_data, evidence_data=None, *, current_fx=None, order_preparation=None, online_preparation=None) -> Preparation:
    capture = BulkCapture.model_validate(capture_data)
    evidence = PricingEvidence.model_validate(evidence_data) if evidence_data is not None else None
    current_fx = CurrentFxEvidence.model_validate(current_fx) if current_fx is not None else None
    live = LiveOrderPreparation.model_validate(order_preparation) if order_preparation is not None else None
    live_online = LiveOnlinePreparation.model_validate(online_preparation) if online_preparation is not None else None
    if live_online is not None and (live is None or evidence is not None):
        raise ValueError('实时在线准备必须绑定实时订单，不能与文件补充证据混用')
    if live is not None and evidence is not None:
        raise ValueError('实时订单准备与文件补充证据不能混用')
    if current_fx is not None and evidence is not None:
        raise ValueError('实时汇率和文件补充证据不能混用')
    business_date = capture.queried_at.astimezone(BEIJING).date()
    groups = {group.source: group for group in capture.groups}
    if len(groups) != len(capture.groups):
        raise ValueError("同一采集来源重复，无法证明订单覆盖")

    export_times = []
    ordered_observations = True
    for group in capture.groups:
        for batch in group.batches:
            times = [poll.queried_at for poll in batch.polls]
            if batch.downloaded_at is not None:
                times.append(batch.downloaded_at)
            if any(earlier > later for earlier, later in pairwise(times)):
                ordered_observations = False
            export_times.extend(times)
    latest_observation = max([capture.queried_at, *export_times])
    valid_observation_window = ordered_observations and all(
        time >= capture.queried_at and time.astimezone(BEIJING).date() == business_date for time in export_times
    )
    if current_fx is not None and (
        current_fx.synthetic != capture.synthetic or current_fx.date != business_date
        or current_fx.queried_at.astimezone(BEIJING).date() != business_date
        or current_fx.queried_at < latest_observation or not valid_observation_window
    ):
        raise ValueError('实时汇率业务使用日、查询时点或样例属性与本次采集不一致')
    fx = evidence.fx if evidence else current_fx
    evaluated_at = evidence.evaluated_at if evidence else (current_fx.queried_at if current_fx else (live.observed_at if live else None))

    scope_by_source = {
        source: group.scope_bloc(capture.department, capture.salesperson) for source, group in groups.items()
    }
    scope_blocs = set(scope_by_source.values())
    blocked_platforms = {str(group.filters.get("ptid")) for group in groups.values() if group.source.startswith("blocked_")}
    scope_matches = len(scope_blocs) == 1 and None not in scope_blocs and len(blocked_platforms) <= 1

    memberships = defaultdict(dict)
    order_finance = defaultdict(list)
    for source, group in groups.items():
        finance = defaultdict(list)
        for row_number, row in enumerate(group.rows, 1):
            finance[row.orderid].append(row)
            margin = row.fields.corrected_first_margin
            order_finance[row.orderid].append({
                "source": source,
                "row_number": row_number,
                "order_status": row.fields.order_status,
                "status_matches_source": row.current_status_matches_source,
                "erp_shop": row.fields.shop,
                "corrected_first_margin": margin if isinstance(margin, str) else None,
            })
        for order_id in group.candidate_order_ids:
            memberships[order_id][source] = finance.get(order_id, [])

    orders = {}
    contracts = {}
    sales_conflicts = set()
    source_versions = {"erp_capture": capture.source_version, "erp_capture_id": capture.capture_id}
    evidence_refs = {}
    if live is not None:
        if (live.erp_capture_id != capture.capture_id or live.synthetic != capture.synthetic
                or live.sample_only != capture.sample_only
                or live.observed_at.astimezone(BEIJING).date() != business_date
                or live.observed_at < latest_observation
                or (current_fx is not None and live.observed_at > current_fx.queried_at)):
            raise ValueError('实时订单准备与采集范围、样本或观察时点不一致')
        orders = {order.order_id:order for order in live.orders}
        if (orders.keys() | live.shop_scopes.keys()) - memberships.keys():
            raise ValueError('实时订单准备包含采集范围外订单')
        for order in live.orders:
            for row in order_finance[order.order_id]:
                try:
                    amount = Decimal(row['corrected_first_margin'])
                    valid = amount.is_finite() and amount == order.lines[0].loss.amount
                except (InvalidOperation, TypeError):
                    valid = False
                if not valid:
                    raise ValueError('实时订单金额与本轮财务观察不一致')
        source_versions.update(details=live.version, mapping=live.version)
        evidence_refs['live_order_sources'] = live.references
    if live_online is not None:
        if (live_online.erp_capture_id != capture.capture_id or live_online.synthetic != capture.synthetic
                or live_online.sample_only != capture.sample_only
                or live_online.observed_at.astimezone(BEIJING).date() != business_date
                or live_online.evaluated_at < live.observed_at
                or live_online.evaluated_at > live_online.observed_at
                or live_online.evaluated_at.astimezone(BEIJING).date() != business_date
                or (current_fx is not None and live_online.observed_at > current_fx.queried_at)):
            raise ValueError('实时在线准备与采集范围、样本或观察时点不一致')
        contracts = {entry.contract.site:entry.contract for entry in live_online.contracts}
        if len(contracts)!=len(live_online.contracts):
            raise ValueError('实时在线准备同站点合同重复')
        identities = [entry.state.key.identity() for entry in live_online.online]
        if len(identities)!=len(set(identities)):
            raise ValueError('实时在线准备存在重复在线身份')
        if any(entry.state.observed_at > live_online.evaluated_at for entry in live_online.online):
            raise ValueError('在线记录观察时间晚于准备完成时间')
        evaluated_at = live_online.evaluated_at
        sales_conflicts = conflicting_main_sku_scopes((entry.state for entry in live_online.online),evaluated_at)
        source_versions['online'] = live_online.version
        evidence_refs['live_online_sources'] = live_online.references
        evidence_refs['online'] = [{'key':entry.state.key.model_dump(mode='json'),'ref':entry.ref}
            for entry in live_online.online]
        evidence_refs['contracts'] = {entry.contract.site:entry.ref for entry in live_online.contracts}
    if evidence:
        if evidence.capture_id != capture.capture_id or evidence.synthetic != capture.synthetic:
            raise ValueError("补充文件与采集编号或样例属性不一致")
        if (
            not valid_observation_window or evidence.evaluated_at.astimezone(BEIJING).date() != business_date
            or evidence.evaluated_at < latest_observation
        ):
            raise ValueError("采集与评估须在同一北京时间业务日，评估时点须晚于已记录的导出观察")
        orders = {order.order_id: order for order in evidence.orders}
        if len(orders) != len(evidence.orders) or orders.keys() - memberships.keys():
            raise ValueError("补充文件包含重复订单或采集范围外订单")
        contracts = {entry.contract.site: entry.contract for entry in evidence.contracts}
        if len(contracts) != len(evidence.contracts):
            raise ValueError("同一站点出现多份合同，需确定适用版本")
        sales_conflicts = conflicting_main_sku_scopes((entry.state for entry in evidence.online), evidence.evaluated_at)
        source_versions.update(evidence.source_versions.model_dump())
        if evidence.fx:
            source_versions["fx"] = evidence.fx.version
        evidence_refs.update(
            online=[{"key": entry.state.key.model_dump(mode="json"), "ref": entry.ref} for entry in evidence.online],
            contracts={entry.contract.site: entry.ref for entry in evidence.contracts},
            fx=evidence.fx.ref if evidence.fx else None,
        )
    if current_fx is not None:
        source_versions['fx'] = current_fx.version
        evidence_refs.update(fx=current_fx.ref, current_fx=current_fx.model_dump(mode='json'))

    issues, normalized, references = [], [], {}
    incomplete_sources, held_targets = set(), set()
    if live is not None:
        shop_ids_by_name = defaultdict(set)
        for scope in live.shop_scopes.values():
            shop_ids_by_name[scope.erp_shop].add(scope.shop_id)
        for order in orders.values():
            for line in order.lines:
                if line.mapping is not None:
                    shop_ids_by_name[line.mapping.erp_shop].add(line.mapping.key.shop_id)
        # 名称与稳定ID冲突意味着别名归属也不可靠，不能只挂其中一个ID。
        if any(len(ids)>1 for ids in shop_ids_by_name.values()):
            incomplete_sources.update(groups)
    for order_id, source_rows in memberships.items():
        order = orders.get(order_id)
        sources = list(source_rows)
        shops = {row.fields.shop for rows in source_rows.values() for row in rows
                 if row.fields.shop and row.fields.shop.strip()}
        order_problem = _order_problem(source_rows, groups, shops)
        scope = live.shop_scopes.get(order_id) if live is not None else None
        # 实时采集已给出候选订单号；原生明细未就绪或缺子 SKU 映射时只排除该单，
        # 不虚构商品键，也不把同店其他订单一起排除。财务缺行排除该单，店铺证据矛盾仍阻断范围。
        order_can_be_skipped = (live is not None and order_problem in (None, 'order_status_conflict', 'missing_order_finance')
                                and (scope is None or shops == {scope.erp_shop}))

        details_problem = None
        if order is None:
            details_problem = "missing_order_details"
        elif (
            not order.details_complete or not order.lines
            or len({line.detail_id for line in order.lines}) != len(order.lines)
        ):
            details_problem = "incomplete_order_details"
        if details_problem:
            if order_problem:
                _add_issue(issues, order_id, sources, order_problem)
            _add_issue(issues, order_id, sources, details_problem)
            if not order_can_be_skipped:
                incomplete_sources.update(sources)
            continue

        if order_problem in {"missing_order_shop", "order_shop_conflict"}:
            # 店铺缺失或互相矛盾时无法验证补充映射，不能只挂起其中一个商品。
            _add_issue(issues, order_id, sources, order_problem)
            incomplete_sources.update(sources)
            continue

        for line in order.lines:
            # 已核实的非亏损单不需要在线商品映射；未知财务或归属仍走异常检查。
            if (live is not None and order_problem is None and len(order.lines) == 1
                    and line.loss is not None and line.loss.amount >= 0 and not line.loss.abnormal
                    and line.loss.allocation_reliable and line.loss.basis == 'order'):
                continue
            if line.mapping is None or (shops and line.mapping.erp_shop not in shops):
                if order_problem:
                    _add_issue(issues, order_id, sources, order_problem, line)
                code = "missing_online_mapping" if line.mapping is None else "mapping_shop_mismatch"
                _add_issue(issues, order_id, sources, code, line)
                if line.mapping is not None or not order_can_be_skipped:
                    incomplete_sources.update(sources)
                continue
            if scope is not None and line.mapping.key.shop_id != scope.shop_id:
                _add_issue(issues, order_id, sources, "mapping_shop_mismatch", line)
                incomplete_sources.update(sources)
                held_targets.add(line.mapping.key.target_identity())
                continue
            if problem := order_problem or _line_problem(line, len(order.lines), sales_conflicts):
                _add_issue(issues, order_id, sources, problem, line)
                # 实时订单状态冲突时，该订单不能作为当前亏损样本；独立正常订单即使同 SKU 也不受影响。
                if live is None or problem not in ('order_status_conflict', 'missing_order_finance'):
                    held_targets.add(line.mapping.key.target_identity())
                continue
            row_id = json.dumps([order_id, line.detail_id], ensure_ascii=False, separators=(",", ":"))
            references[row_id] = {
                "order_id": order_id, "detail_id": line.detail_id, "detail_ref": line.detail_ref,
                "mapping_ref": line.mapping.ref, "erp_shop": line.mapping.erp_shop, "loss_ref": line.loss.ref,
                "target": line.mapping.key.model_dump(mode="json"), "main_sku": line.mapping.main_sku,
                # 留存证据合同的原币和金额粒度，缺汇率时也能核对明细归属。
                "loss_input": {
                    "quantity": str(line.loss.quantity), "amount": str(line.loss.amount),
                    "currency": line.loss.currency, "basis": line.loss.basis,
                },
            }
            normalized.append((row_id, order_id, sources, line))

    lines = []
    for row_id, order_id, sources, line in normalized:
        if line.mapping.key.target_identity() in held_targets:
            _add_issue(issues, order_id, sources, "target_held", line)
            continue
        lines.extend(_source_line(capture, order_id, source, row_id, line) for source in sources)

    if live_online is not None:
        retained_keys={line.key.identity() for line in lines}
        if any(entry.state.key.identity() not in retained_keys for entry in live_online.online):
            raise ValueError('实时在线准备包含已挂起或不在规范候选范围的在线身份')
        issues.extend(live_online.issues)

    online_entries = live_online.online if live_online is not None else (evidence.online if evidence else [])
    online_states = []
    for entry in online_entries:
        state = entry.state
        if capture.department is not None and (
            state.profit_check is None or state.profit_check.department.casefold() != capture.department.casefold()
        ):
            # 商品可能换过部门。同一目标只能由当前在线商品归属的部门出价。
            issues.append({"code": "online_department_mismatch",
                           "reason": "在线商品部门缺失或与本次采集部门不一致",
                           "target": state.key.model_dump(mode="json")})
            continue
        online_states.append(state)

    finance_coverage = {
        source: group.finance_coverage(scope_by_source[source], capture.department, capture.salesperson)
        for source, group in groups.items()
    }
    complete_sources = [
        source for source in SOURCES
        if any(finance_coverage.get(source, (False, False))) and source not in incomplete_sources
        and scope_matches and valid_observation_window and not capture.sample_only
    ]
    coverage = [
        {
            "source": source, "candidate_orders": len(groups[source].candidate_order_ids) if source in groups else None,
            "capture_stage": groups[source].stage if source in groups else None,
            "list_complete": groups[source].list_complete if source in groups else False,
            "finance_rows": len(groups[source].rows) if source in groups else None,
            "finance_complete": finance_coverage.get(source, (False, False))[0],
            "preparation_complete": source in complete_sources,
        }
        for source in SOURCES
    ]
    global_issues = []
    if not scope_matches:
        global_issues.append("采集来源的筛选范围不一致或无法确认")
    if not valid_observation_window:
        global_issues.append("财务导出跨业务日或时点次序异常")
    for source, group in groups.items():
        if (
            not capture.sample_only and scope_by_source[source] is not None
            and group.stage == "complete" and not finance_coverage[source][0]
        ):
            global_issues.append(f"{source} 标记完成，但候选、财务行或子批证据未能对齐")
    if len(complete_sources) != len(SOURCES):
        unavailable_lists = [f"{'亏损订单' if kind == 'loss' else '禁止发货'} × {STATUS_LABELS[status]}"
                             for source, (kind, status) in SOURCES.items()
                             if source not in groups or not groups[source].list_complete]
        if unavailable_lists:
            global_issues.append(f"候选名单未取全：{'、'.join(unavailable_lists)}；不能按零订单算价")
        else:
            global_issues.append("四类候选覆盖或明细归属仍有未定位缺口")
    if fx is None:
        global_issues.append("缺少带日期及来源引用的汇率，尚未进入算价")

    report = {
        "schema_version": "erp-pricing-preview-v1",
        "capture_id": capture.capture_id,
        "capture_version": capture.source_version,
        "captured_at": capture.queried_at.isoformat(),
        "latest_capture_observation_at": latest_observation.isoformat(),
        "business_date": business_date.isoformat(),
        "evaluated_at": evaluated_at.isoformat() if evaluated_at else None,
        "synthetic": capture.synthetic,
        "execution_allowed": False,
        "consistent_capture_scope": scope_matches,
        "source_coverage": coverage,
        "evaluation_complete_sources": complete_sources,
        "summary": {
            "source_rows": sum(len(group.rows) for group in groups.values()),
            "unique_orders": len(memberships),
            "normalized_detail_lines": len({line.row_id for line in lines}),
            "preparation_issues": len(issues),
        },
        "global_issues": global_issues,
        "issues": issues,
        "source_versions": source_versions,
        "evidence_version": evidence.schema_version if evidence else None,
        "live_preparation_version": live.version if live else None,
        "references": {"lines": references, "retained_row_ids": sorted({line.row_id for line in lines}),
                       "order_finance": dict(order_finance), **evidence_refs},
    }
    if capture.department is not None:
        report["department"] = capture.department
    if fx is None:
        return Preparation(report, None, None)

    snapshot = Snapshot(
        as_of=max([latest_observation, live.observed_at, *(entry.state.observed_at for entry in live_online.online)]) if live_online else (max(latest_observation, live.observed_at) if live else latest_observation), business_date=business_date,
        fx_date=fx.date, fx_rates=fx.source_rates if current_fx is not None else fx.rates,
        fx_rate_unit='cny_per_currency' if current_fx is not None else 'currency_per_cny',
        complete_sources=complete_sources, lines=lines, online=online_states,
        contracts=contracts, source_versions=source_versions,
    )
    return Preparation(report, snapshot, evaluated_at)


def _order_problem(source_rows, groups, shops):
    if len(shops) > 1:
        return "order_shop_conflict"
    if any(source_rows.values()) and not shops:
        return "missing_order_shop"
    if any(not rows for rows in source_rows.values()):
        return "missing_order_finance"
    if any(len(rows) > 1 for rows in source_rows.values()):
        return "duplicate_finance_rows"
    statuses = {groups[source].status for source in source_rows}
    if len(statuses) != 1 or any(
        not row.current_status_matches_source or row.fields.order_status != STATUS_LABELS.get(groups[source].status)
        for source, rows in source_rows.items() for row in rows
    ):
        return "order_status_conflict"
    return None


def _line_problem(line, detail_count, sales_conflicts):
    if line.mapping.key.sales_scope(line.mapping.main_sku) in sales_conflicts:
        return "conflicting_main_sku_sales"
    if line.loss is None:
        return "missing_line_loss"
    if line.loss.basis == "order" and (detail_count != 1 or not line.loss.allocation_reliable):
        return "unreliable_order_loss"
    return None


def _add_issue(issues, order_id, sources, code, line=None):
    issue = {"order_id": order_id, "sources": sources, "code": code, "reason": ISSUE_REASONS[code]}
    if line:
        issue.update(detail_id=line.detail_id, detail_ref=line.detail_ref)
        if line.mapping:
            issue.update(target=line.mapping.key.model_dump(mode="json"), mapping_ref=line.mapping.ref)
    issues.append(issue)


def _source_line(capture, order_id, source, row_id, line):
    # 财务表的修正一次毛利是订单级，item ID 与商品 SKU 列表也只供回查。
    # 此处只使用已归属的补充证据，保留负号、原币、件数及金额粒度，由引擎单件化。
    return SourceLine(
        row_id=row_id, order_id=order_id, source=source, key=line.mapping.key,
        main_sku=line.mapping.main_sku, quantity=line.loss.quantity, loss_amount=line.loss.amount,
        currency=line.loss.currency, basis=line.loss.basis, allocation_reliable=line.loss.allocation_reliable,
        fee_scope=line.loss.fee_scope, abnormal=line.loss.abnormal,
        source_ref=f"{capture.capture_id}/{source}/{order_id}",
    )
