from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, StrictInt, StrictStr


Site = Literal["MY", "PH", "SG", "TH", "VN"]
Source = Literal["loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"]
PriceRounding = Literal["ROUND_HALF_UP", "ROUND_DOWN", "ROUND_UP"]
DecisionStatus = Literal["本轮不调", "待处理", "待核验", "已核验"]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]


class VariantKey(BaseModel):
    """商品 ID 与 SKU 按原始文本保留，避免数值化丢失前导零。"""

    platform: StrictStr
    shop_id: StrictStr
    site: Site
    product_id: StrictStr
    item_id: StrictStr
    sku: StrictStr
    warehouse: StrictStr

    def identity(self) -> str:
        """来源与在线 SKU 严格匹配身份；保留 SKU，不能用执行锁身份合并亏损样本。"""
        fields = (self.platform, self.shop_id, self.site, self.product_id, self.item_id, self.sku)
        return sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()

    def target_identity(self) -> str:
        """实际上传对象的稳定身份。来源 SKU 改名、仓配变化不允许绕过未知锁或72小时保护。"""
        fields = (self.platform, self.shop_id, self.site, self.product_id, self.item_id)
        return sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()

    def grouping(self) -> str:
        """算价分组包含仓配口径，单个在线子 SKU 出现多个仓配时交由核对。"""
        fields = (self.identity(), self.warehouse)
        return sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()


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
    allocation_reliable: bool = True
    fee_scope: StrictStr = "normal"
    abnormal: bool = False
    source_ref: StrictStr = ""


class FeeModel(BaseModel):
    """经确认的一次毛利模型，系数无量纲，固定项和输出均为站点币。"""

    revenue_factor: FiniteDecimal
    fixed_cost: FiniteDecimal
    version: StrictStr
    verified: bool = False

    def profit(self, price: Decimal) -> Decimal:
        """输入站点币实际售价，只核算正常一次毛利，排除广告和营销费用。"""
        return price * self.revenue_factor - self.fixed_cost


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
    observed_at: AwareDatetime
    sales_7d: StrictInt | None = None
    sales_start: date
    sales_end: date
    sales_complete: bool = True
    history_known: bool = True
    # 只接受已确认生效的时间戳。提交结果未知由 pending_submission 独立锁定。
    last_increase_at: AwareDatetime | None = None
    pending_submission: bool = False
    protected: bool = False
    activity_locked: bool = False
    sellable: bool = True


class SiteContract(BaseModel):
    """站点合同定义上传字段、价格档位与舍入规则，由数据提供方明确确认。"""

    site: Site
    verified: bool = False
    price_step: FiniteDecimal
    rounding: PriceRounding
    upload_column: StrictStr
    version: StrictStr


class Snapshot(BaseModel):
    """单批规范化来源。业务日期和汇率日期均按北京时间，汇率为每 CNY 兑换的币种单位。"""

    as_of: AwareDatetime
    business_date: date
    fx_date: date
    fx_rates: dict[str, FiniteDecimal]
    lines: list[SourceLine]
    online: list[OnlineState]
    contracts: dict[str, SiteContract]
    complete_sources: list[str]
    source_versions: dict[str, str] = Field(default_factory=dict)


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
