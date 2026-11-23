"""用已核对的费用分项和公司报价构建完整在线利润模型。"""
from decimal import Decimal

from .money import MoneyRatio
from .types import FeeModel


def direct_freight(weight_g: Decimal, base_weight_g: Decimal, base_price: MoneyRatio,
                   step_price: MoneyRatio, step_weight_g: Decimal) -> MoneyRatio:
    """按销售系统首续重模板计算每件站点币运费；重量单位g，超首重按续重单位向上进档。"""
    excess = MoneyRatio(weight_g) - MoneyRatio(base_weight_g)
    steps = ((excess / MoneyRatio(step_weight_g)).round_to_step(Decimal(1), 'ROUND_UP')
             if excess > MoneyRatio(Decimal(0)) else Decimal(0))
    return base_price + MoneyRatio(steps) * step_price


def online_fee_model(*, total_rate_percent: MoneyRatio, fixed_cny: MoneyRatio,
                     vat_percent: MoneyRatio, cny_per_currency: Decimal, version: str) -> FeeModel:
    """总费率以百分数传入，含基础平台费及营销费，已剔除额外公开计划佣金。

    每件固定成本以 CNY 计入 VAT，模型保存原始 CNY/站点币报价作为除数。
    利润门槛由定价引擎应用。verified 表示当前费用输入已按来源合同核对。
    """
    percent = MoneyRatio(Decimal('0.01'))
    factor = (MoneyRatio(Decimal(100)) - total_rate_percent) * percent
    taxed_fixed = fixed_cny * (MoneyRatio(Decimal(1)) + vat_percent * percent)
    return FeeModel(revenue_factor=factor.decimal(), fixed_cost=taxed_fixed.decimal(),
                    fixed_cost_divisor=cny_per_currency, version=version, verified=True)
