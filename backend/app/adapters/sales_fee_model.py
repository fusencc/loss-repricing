"""公司实际海外仓一次毛利分项的规范化；先正向对账，再排除营销费用。

依据：2026-11-05公司口径及11-11真实展开库存、海外费率和两条负毛利复算。
费用层级选择、来源时间/集团归属由调用者另行证明。未知成本、多个有效仓或
公司展示毛利不能被已知分项复现时不生成审核模型；不从取整毛利反推成本。
"""
from decimal import Decimal
import json

from app.adapters.normalization import money
from app.domain.engine import SITE_CURRENCIES
from app.domain.money import MoneyRatio
from app.domain.types import FeeModel

PERCENT_FIELDS = ('costOfCapital','procedureRate','platformRate','returnRate',
                  'warehouseStorageRate','marketingRate','otherRate')
COST_FIELDS = ('packageFee','warehouseCost','warehouseCostDomestic','overseaPackageFee',
               'publishFee','dischargeFee','orderProcessFee')
VERSION = 'sales-overseas-normal-cny-v1'


def _positive(value):
    value = money(value)
    if value <= 0:
        raise ValueError('海外仓报价、数量或实际售价不是正数')
    return value


def _nonnegative(value):
    value = money(value)
    if value < 0:
        raise ValueError('海外仓费用包含尚未核对的负分项')
    return MoneyRatio(value)


def _sum(values):
    return sum(values, MoneyRatio(Decimal(0)))


def compile_overseas_fee(variant, rate, *, site, warehouse, company_cny_per_currency):
    if site not in SITE_CURRENCIES or rate.get('siteCode') != site or variant.get('currency') != SITE_CURRENCIES[site]:
        raise ValueError('海外仓费率或币种不属于该站点')
    quote = _positive(company_cny_per_currency)
    actual = _positive(variant.get('marketPrice'))
    stock = _positive(variant.get('overseasStockSum'))
    raw_stock = variant.get('overseasStockWarehouseInfo')
    if not isinstance(raw_stock, str):
        raise ValueError('缺少海外仓库存原始字段')
    buckets = json.loads(raw_stock, parse_float=Decimal)
    if not isinstance(buckets, list) or any(not isinstance(row, dict) for row in buckets):
        raise ValueError('海外仓库存结构无效')
    quantities = [_nonnegative(row.get('quantity')) for row in buckets]
    positive = [row for row, quantity in zip(buckets, quantities) if quantity.numerator > 0]
    if (len(positive) != 1 or positive[0].get('overseasWarehouses') != [warehouse]
            or _sum(quantities) != MoneyRatio(stock)):
        raise ValueError('当前海外仓库存或仓配归属不唯一')
    bucket = positive[0]
    procurement = bucket.get('jqCostpriceList')
    freight = bucket.get('jqFreightList')
    if not isinstance(procurement, list) or not isinstance(freight, list) or len(procurement) != 1 or len(freight) != 1:
        raise ValueError('海外仓采购成本或头程费用未唯一确认')
    rates = {key:_nonnegative(rate.get(key)) for key in PERCENT_FIELDS}
    costs = _sum(_nonnegative(rate.get(key)) for key in COST_FIELDS)
    total = _sum(rates.values())
    if total != MoneyRatio(money(rate.get('totalRate'))) or costs != MoneyRatio(money(rate.get('totalCost'))):
        raise ValueError('海外仓费率分项与公司合计不一致')
    fixed = costs + _nonnegative(procurement[0]) + _nonnegative(freight[0])
    all_factor = (MoneyRatio(Decimal(100)) - total) * MoneyRatio(Decimal('0.01'))
    normal_factor = all_factor + rates['marketingRate'] * MoneyRatio(Decimal('0.01'))
    included_model = FeeModel(revenue_factor=all_factor.numerator, fixed_cost=fixed.numerator,
        fixed_cost_divisor=quote, version=VERSION)
    included_percent = included_model.profit_exact(MoneyRatio(actual)) / MoneyRatio(actual) * MoneyRatio(Decimal(100))
    reported = money(variant.get('currentOverseasProfitMargin'))
    if (included_percent - MoneyRatio(reported)).absolute() > MoneyRatio(Decimal('0.005')):
        raise ValueError('海外仓分项公式未复现当前公司毛利，须核对报价或快照')
    model = FeeModel(revenue_factor=normal_factor.numerator, fixed_cost=fixed.numerator,
        fixed_cost_divisor=quote, version=VERSION, verified=True)
    return {'model':model, 'current_margin':(model.profit_exact(MoneyRatio(actual)) / MoneyRatio(actual)).decimal(),
        'proof':{'version':VERSION,'fixed_cost_currency':'CNY','company_quote_unit':'CNY/'+variant['currency'],
            'company_quote':format(quote,'f'),'fixed_cost':format(fixed.numerator,'f'),
            'reported_margin_percent':format(reported,'f'),
            'recalculated_including_marketing_percent':str(included_percent.decimal()),
            'excluded_percent_fields':['marketingRate'],'warehouse':warehouse}}
