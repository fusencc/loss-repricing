from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, StrictInt, StrictStr

from .money import MoneyRatio


Site = Literal["MY", "PH", "SG", "TH", "VN"]
Source = Literal["loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"]
PriceRounding = Literal["ROUND_HALF_UP", "ROUND_DOWN", "ROUND_UP"]
DecisionStatus = Literal["本轮不调", "待处理", "待核验", "已核验"]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]


class VariantReference(BaseModel):
    """已定位的来源变体。字段异常记录可以保留仓配缺失的原状。"""

    platform: StrictStr
    shop_id: StrictStr
    site: Site
    product_id: StrictStr
    item_id: StrictStr
    sku: StrictStr
    warehouse: StrictStr | None

    def identity(self) -> str:
        """来源与在线 SKU 严格匹配身份；保留 SKU，不能用执行锁身份合并亏损样本。"""
        fields = (self.platform, self.shop_id, self.site, self.product_id, self.item_id, self.sku)
        return sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()

    def target_identity(self) -> str:
        """实际上传对象的稳定身份。来源 SKU 改名、仓配变化不允许绕过未知锁或72小时保护。"""
        fields = (self.platform, self.shop_id, self.site, self.product_id, self.item_id)
        return sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()

    def sales_scope(self, main_sku: str) -> tuple[str, str, Site, str]:
        return self.platform, self.shop_id, self.site, main_sku

    def grouping(self) -> str:
        """算价分组包含仓配口径，单个在线子 SKU 出现多个仓配时交由核对。"""
        fields = (self.identity(), self.warehouse)
        return sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()


class VariantKey(VariantReference):
    """商品 ID 与 SKU 按原始文本保留，避免数值化丢失前导零。"""

    warehouse: StrictStr


class SourceLine(BaseModel):
    """四份 ERP 候选导出之一。亏损额的符号、数量粒度和原始币种在归一化前保留。"""

    row_id: StrictStr = ""
    order_id: StrictStr
    source: Source
    key: VariantKey
    main_sku: StrictStr
    quantity: FiniteDecimal
    loss_amount: FiniteDecimal
    currency: StrictStr
    basis: Literal["unit", "line", "order"]
    # 缺少证据即未确认；仅整单粒度依赖该标记，不能默认为可可靠分摊。
    allocation_reliable: bool = False
    fee_scope: StrictStr = "normal"
    abnormal: bool = False
    source_ref: StrictStr = ""


class FeeModel(BaseModel):
    """一次毛利系数无量纲，固定成本按明确比值换成站点币，输出仍为站点币。"""

    revenue_factor: FiniteDecimal
    fixed_cost: FiniteDecimal
    # 原币固定成本 / (原币/站点币)。已用站点币计成本时单位比值为1。
    # 不预先截断循环小数成本；公司CNY报价必须由同一费用来源明确提供。
    fixed_cost_divisor: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] = Decimal(1)
    version: StrictStr
    verified: bool = False

    def profit(self, price: Decimal) -> Decimal:
        """输入站点币实际售价，只核算正常一次毛利，排除广告和营销费用。"""
        return self.profit_exact(MoneyRatio(price)).decimal()

    def profit_exact(self, price: MoneyRatio) -> MoneyRatio:
        """G(P)=P×a-b；循环小数价格延迟除法，零毛利不能由近似尾差变成负值。"""
        return price * MoneyRatio(self.revenue_factor) - MoneyRatio(self.fixed_cost, self.fixed_cost_divisor)


class OnlineProfitCheck(BaseModel):
    """11/12新增的完整在线利润校验，与排除广告营销的一次毛利分开。

    库存为当前目标的海外可售件数；0明确选直发，正数选海外仓。模型以站点币
    实际售价输入，保留部门底表及库存来源，不使用切价售价对应的利润率。
    """

    overseas_stock: FiniteDecimal
    mode: Literal["direct", "overseas"]
    department: StrictStr
    inventory_observed_at: AwareDatetime
    source_ref: StrictStr
    model: FeeModel

    def problem(self, now, business_date):
        from .engine import BEIJING
        selected = "overseas" if self.overseas_stock > 0 else "direct"
        if self.overseas_stock < 0 or self.mode != selected:
            return "海外仓库存与利润校验仓配不一致"
        if (not self.department.strip() or not self.source_ref.strip()
                or self.inventory_observed_at > now
                or self.inventory_observed_at.astimezone(BEIJING).date() != business_date):
            return "库存日期、部门或利润公式来源未确认"
        if (not self.model.verified or not self.model.version.strip()
                or self.model.revenue_factor <= 0 or self.model.fixed_cost < 0):
            return "在线利润模型未确认或不能求得保本价"
        return None

    def break_even(self):
        return MoneyRatio(self.model.fixed_cost, self.model.fixed_cost_divisor) / MoneyRatio(self.model.revenue_factor)


class OnlineState(BaseModel):
    """在线子 SKU 的当前快照。含 VAT 零售价和营销实际售价均为站点币。"""

    key: VariantKey
    main_sku: StrictStr
    currency: StrictStr
    retail_price: FiniteDecimal
    # 没有活动折扣时来源须传 None。零元售价由引擎识别为异常数据。
    marketing_price: FiniteDecimal | None = None
    current_margin: FiniteDecimal
    fee_scope: StrictStr
    fee_model: FeeModel | None = None
    # 缺少新证据仅能展示/排除，不能生成可提交价格；禁止回退到一次毛利模型。
    profit_check: OnlineProfitCheck | None = None
    observed_at: AwareDatetime
    sales_7d: StrictInt | None = None
    # 网页七日指标无明确起止日期；保存实际来源口径与采集时点，不编造自然日窗口。
    sales_basis: Literal['sales_system_web_sku_seven_days'] | None = None
    sales_observed_at: AwareDatetime | None = None
    # 完整销量与确证历史必须由来源明示；文件缺字段不能获得乐观放行。
    sales_complete: bool = False
    history_known: bool = False
    # 只接受已确认生效的时间戳。提交结果未知由 pending_submission 独立锁定。
    last_increase_at: AwareDatetime | None = None
    pending_submission: bool = False
    protected: bool = False
    activity_locked: bool = False
    sellable: bool = True
    # 接口确实提供时才映射；缺失不推断没有人工改价，也不猜测操作者。
    price_version: StrictStr | None = None
    last_changed_at: AwareDatetime | None = None


class SiteContract(BaseModel):
    """站点合同定义上传字段、价格档位与舍入规则，由数据提供方明确确认。"""

    site: Site
    verified: bool = False
    price_step: FiniteDecimal
    rounding: PriceRounding
    upload_column: StrictStr
    version: StrictStr
    # 同一站点不代表所有仓配可用同一税价列；空范围关闭算价，值由已确认来源合同提供。
    warehouses: list[StrictStr] = Field(default_factory=list)


class CandidateSourceIssue(BaseModel):
    """可定位的候选明细字段异常。保留行身份用于去重和整单亏损归属检查。"""

    source: Source
    record_number: Annotated[StrictInt, Field(gt=0)]
    key: VariantReference
    order_id: StrictStr
    row_id: StrictStr


class OnlineSourceIssue(BaseModel):
    """可定位的在线记录字段异常。主 SKU 是七天销量保护的共享范围。"""

    source: Literal["online"]
    record_number: Annotated[StrictInt, Field(gt=0)]
    key: VariantReference
    main_sku: StrictStr
    related_main_skus: list[StrictStr] = Field(default_factory=list)

    def sales_scopes(self) -> Iterator[tuple[str, str, Site, str]]:
        for main_sku in (self.main_sku, *self.related_main_skus):
            yield self.key.sales_scope(main_sku)


SourceIssue = Annotated[CandidateSourceIssue | OnlineSourceIssue, Field(discriminator="source")]


class Snapshot(BaseModel):
    """单批规范来源；日期按北京时间，汇率必须携带明确单位后参与精确比值计算。"""

    as_of: AwareDatetime
    business_date: date
    fx_date: date
    fx_rates: dict[str, FiniteDecimal]
    # 文件规范单位为原币/CNY；ERP当前配置为CNY/原币，保留原值避免循环倒数回读。
    fx_rate_unit: Literal['currency_per_cny', 'cny_per_currency'] = 'currency_per_cny'
    lines: list[SourceLine]
    online: list[OnlineState]
    contracts: dict[str, SiteContract]
    complete_sources: list[str]
    source_versions: dict[str, str] = Field(default_factory=dict)
    source_issues: list[SourceIssue] = Field(default_factory=list)


class Decision(BaseModel):
    """单个在线子 SKU 的判定。提价建议仍处于待处理，ready 标记算价通过。"""

    key: VariantKey
    status: DecisionStatus
    ready: bool
    reason_code: str
    reason: str
    source_row_ids: list[str]
    selected_row_id: str | None = None
    unit_loss: FiniteDecimal | None = None
    loss_currency: str | None = None
    station_loss: FiniteDecimal | None = None
    current_actual: FiniteDecimal | None = None
    discount: FiniteDecimal | None = None
    target_actual: FiniteDecimal | None = None
    upload_price: FiniteDecimal | None = None
    predicted_profit: FiniteDecimal | None = None
    upload_column: str | None = None
    trace: dict[str, Any] = Field(default_factory=dict)
