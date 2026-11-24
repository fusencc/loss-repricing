from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, StrictInt, StrictStr

from .money import MoneyRatio
from .price_reference import SkuPriceReference


Site = Literal["MY", "PH", "SG", "TH", "VN"]
Source = Literal["loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"]
PriceRounding = Literal["ROUND_HALF_UP", "ROUND_DOWN", "ROUND_UP"]
DecisionStatus = Literal["本轮不调", "待处理", "待核验", "已核验"]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]

# 2026-11-20 用户确认：完整在线利润率统一最低 1%，直发与海外仓共用。
ONLINE_PROFIT_MINIMUM_MARGIN = Decimal("0.01")


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


@dataclass(frozen=True)
class OnlineProfitResult:
    """一次试价的站点币单件利润、利润率和 1% 门槛判定，均保留精确比值。"""

    profit: MoneyRatio
    margin: MoneyRatio | None
    meets_minimum_margin: bool


class FeeModel(BaseModel):
    """完整在线利润系数无量纲，固定成本按明确比值换成站点币，输出仍为站点币。"""

    revenue_factor: FiniteDecimal
    fixed_cost: FiniteDecimal
    # 原币固定成本 / (原币/站点币)。已用站点币计成本时单位比值为1。
    # 不预先截断循环小数成本；公司CNY报价必须由同一费用来源明确提供。
    fixed_cost_divisor: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] = Decimal(1)
    version: StrictStr
    verified: bool = False

    def evaluate(self, price: MoneyRatio) -> OnlineProfitResult:
        """用 BD 直发 AU / 海外仓 AV 的口径试算拟定营销价，利润率 = 收入保留比例 − 单件成本 / 营销价。"""
        revenue_factor = MoneyRatio(self.revenue_factor)
        station_cost = MoneyRatio(self.fixed_cost, self.fixed_cost_divisor)
        if price.numerator <= 0:
            # 参考上限可能压至零档位。保留其利润额用于诊断，利润率无定义，由算价规则拒绝。
            return OnlineProfitResult(profit=price * revenue_factor - station_cost,
                                      margin=None, meets_minimum_margin=False)
        margin = revenue_factor - station_cost / price
        return OnlineProfitResult(profit=price * margin, margin=margin,
                                  meets_minimum_margin=margin >= MoneyRatio(ONLINE_PROFIT_MINIMUM_MARGIN))


class ProfitCheckIssue(Enum):
    STOCK_MODE = "海外仓库存与利润校验仓配不一致"
    SOURCE = "库存日期、部门或利润公式来源未确认"
    MODEL = "在线利润模型未确认或不能计算在线利润校正额"


class OnlineProfitCheck(BaseModel):
    """按当前海外库存选择仓配的完整在线利润校验证据。

    库存为当前目标的海外可售件数；0或缺失选直发，正数选海外仓；缺失保留告警，不填0。模型以站点币
    实际售价输入，保留部门底表及库存来源，不使用切价售价对应的利润率。
    """

    overseas_stock: FiniteDecimal | None = None
    mode: Literal["direct", "overseas"]
    department: StrictStr
    inventory_observed_at: AwareDatetime
    source_ref: StrictStr
    model: FeeModel

    def warnings(self) -> list[str]:
        """缺库存是经授权的选模假设，告警留证但不作为待处理原因。"""
        return ["海外库存缺失，已按直发模型计算，请核实库存"] if self.overseas_stock is None else []

    def validation_issue(
        self, now: datetime, business_date: date, *, minimum_margin_required: bool = True,
    ) -> ProfitCheckIssue | None:
        from .engine import BEIJING
        # 2026-11-20用户确认：缺库存用直发；负数和仓配冲突仍阻断。
        selected = "overseas" if self.overseas_stock is not None and self.overseas_stock > 0 else "direct"
        if (self.overseas_stock is not None and self.overseas_stock < 0) or self.mode != selected:
            return ProfitCheckIssue.STOCK_MODE
        if (not self.department.strip() or not self.source_ref.strip()
                or self.inventory_observed_at > now
                or self.inventory_observed_at.astimezone(BEIJING).date() != business_date):
            return ProfitCheckIssue.SOURCE
        if (not self.model.verified or not self.model.version.strip() or self.model.fixed_cost < 0
                or (minimum_margin_required and self.model.revenue_factor <= ONLINE_PROFIT_MINIMUM_MARGIN)):
            return ProfitCheckIssue.MODEL
        return None

    def minimum_actual_price(self) -> MoneyRatio | None:
        """求满足 (a - m) × P ≥ b 的价格下限。无正价能达到时返回 None。"""
        factor = self.model.revenue_factor
        if factor == ONLINE_PROFIT_MINIMUM_MARGIN and self.model.fixed_cost == 0:
            return MoneyRatio(Decimal(0))  # 每个正价都恰好达到 1%。
        if factor <= ONLINE_PROFIT_MINIMUM_MARGIN:
            return None
        station_cost = MoneyRatio(self.model.fixed_cost, self.model.fixed_cost_divisor)
        return station_cost / (MoneyRatio(factor) - MoneyRatio(ONLINE_PROFIT_MINIMUM_MARGIN))


class RecentPriceLog(BaseModel):
    """开放接口按子商品ID返回的72小时日志数量；不伪造最近生效时间。"""

    count: Annotated[StrictInt, Field(ge=0)]
    window_start: AwareDatetime
    window_end: AwareDatetime
    source_id: StrictStr


class OnlineState(BaseModel):
    """在线子 SKU 的当前快照。含 VAT 零售价和营销实际售价均为站点币。"""

    key: VariantKey
    main_sku: StrictStr
    currency: StrictStr
    retail_price: FiniteDecimal
    # 没有活动折扣时来源须传 None。零元售价由引擎识别为异常数据。
    marketing_price: FiniteDecimal | None = None
    # 可选同SKU营销均价，有值时限制最高营销价；没有Top3或查询失败不新增限制。
    sku_price_reference: SkuPriceReference | None = None
    fee_scope: StrictStr
    # 当前费用模型用于补亏候选价、进档价及回读价的完整在线利润校验。
    profit_check: OnlineProfitCheck | None = None
    observed_at: AwareDatetime
    history_known: bool = False
    recent_price_log: RecentPriceLog | None = None
    # 只接受已确认生效的时间戳。提交结果未知由 pending_submission 独立锁定。
    last_increase_at: AwareDatetime | None = None
    pending_submission: bool = False
    protected: bool = False
    activity_locked: bool = False
    sellable: bool = True
    # 接口确实提供时才映射；缺失不推断没有人工改价，也不猜测操作者。
    price_version: StrictStr | None = None
    last_changed_at: AwareDatetime | None = None

    def actual_price_and_discount(self) -> tuple[Decimal, MoneyRatio] | None:
        """缺营销价时按含税零售价成交。有效成交比例 P/R 保留精确分数。"""
        actual = self.marketing_price if self.marketing_price is not None else self.retail_price
        if actual <= 0 or actual > self.retail_price:
            return None
        return actual, MoneyRatio(actual, self.retail_price)


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
    """可定位的在线字段异常；主 SKU 供管理员回查，不扩大字段异常的阻断范围。"""

    source: Literal["online"]
    record_number: Annotated[StrictInt, Field(gt=0)]
    key: VariantReference
    main_sku: StrictStr
    related_main_skus: list[StrictStr] = Field(default_factory=list)


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
    # 当前公式版本：档位后库存对应完整在线利润额，站点币/件。历史值按原公式版本解释。
    predicted_profit: FiniteDecimal | None = None
    upload_column: str | None = None
    trace: dict[str, Any] = Field(default_factory=dict)
