"""用户固定BD表费用公式：费率用于库存对应完整在线利润。

当前一次毛利仅计算现价筛选观测值，不输出独立新价保本模型。

固定费率与商品输入由fixed_pricing_fees绑定版本；库存、成本、重量与报价由
当前来源提供。未知成本、多个有效海外仓或分项不完整时不生成模型。
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
    # 用户固定BD表AV公式：VAT作用在固定成本上，公开计划佣金独立从收入扣除。
    # 不采用工作簿的缓存利润率；表中12%/12.5%目标也不覆盖本程序0%保本规则。
    vat = _nonnegative(rate.get('vatRate')) * MoneyRatio(Decimal('0.01'))
    commission = _nonnegative(rate.get('publicCommissionRate')) * MoneyRatio(Decimal('0.01'))
    taxed_fixed = fixed * (MoneyRatio(Decimal(1)) + vat)
    included_factor = all_factor - commission
    version = rate.get('version')
    if not isinstance(version, str) or not version.strip():
        raise ValueError('固定费率缺少版本')
    included_model = FeeModel(revenue_factor=included_factor.decimal(), fixed_cost=taxed_fixed.decimal(),
        fixed_cost_divisor=quote, version=version)
    included_percent = included_model.profit_exact(MoneyRatio(actual)) / MoneyRatio(actual) * MoneyRatio(Decimal(100))
    # 11/12明确使用部门统一费率，而列表显示值可能使用个人/店铺覆盖费率。
    # 显示值只保留作对账，不能据其倒推成本或要求与部门模型强行相等。
    reported = variant.get('currentOverseasProfitMargin')
    included_model.verified = True
    model = FeeModel(revenue_factor=normal_factor.numerator, fixed_cost=fixed.numerator,
        fixed_cost_divisor=quote, version=version, verified=True)
    return {'online_model':included_model, 'current_margin':(model.profit_exact(MoneyRatio(actual)) / MoneyRatio(actual)).decimal(),
        'proof':{'version':version,'fixed_cost_currency':'CNY','company_quote_unit':'CNY/'+variant['currency'],
            'company_quote':format(quote,'f'),'fixed_cost':format(fixed.numerator,'f'),
            'reported_margin_percent':None if reported is None else str(reported),
            'online_profit_includes_marketing':True,
            'recalculated_including_marketing_percent':str(included_percent.decimal()),
            'vat_percent':str(rate['vatRate']), 'public_commission_percent':str(rate['publicCommissionRate']),
            'fixed_rate_ref':rate.get('source_ref'), 'source_cells':rate.get('source_cells'),
            'excluded_percent_fields':['marketingRate'],'warehouse':warehouse}}


def compile_direct_fee(variant, rate, *, site, company_cny_per_currency):
    """固定BD表AU/AE公式：g重量分档，跨境运费先按站点币计，再折为CNY成本。

    TH类目VAT复用当前在线父商品字段；SG缺公共费率即关闭，不猜税率或运费。
    公式没有使用AD固定运费；配置非0时要求先明确公式，不能悄悄丢掉。
    """
    if site not in SITE_CURRENCIES or variant.get('currency') != SITE_CURRENCIES[site]:
        raise ValueError('直发币种与站点不一致')
    if money(variant.get('overseasStockSum')) != 0:
        raise ValueError('有海外库存不能使用直发公式')
    quote, actual = _positive(company_cny_per_currency), _positive(variant.get('marketPrice'))
    weight = _positive(variant.get('localWeight'))
    freight = rate['freight']
    base_weight = _positive(freight['base_weight_g'])
    excess = MoneyRatio(weight) - MoneyRatio(base_weight)
    steps = ((excess / MoneyRatio(Decimal(10))).round_to_step(Decimal(1), 'ROUND_UP')
             if excess > MoneyRatio(Decimal(0)) else Decimal(0))
    freight_station = _nonnegative(freight['base_price']) + MoneyRatio(steps) * _nonnegative(freight['per_10g_price'])
    if money(rate.get('fixedFreight')) != 0:
        raise ValueError('固定运费非0，须确认其在BD直发公式中的位置')
    fixed = (_nonnegative(variant.get('costPrice')) + _nonnegative(rate.get('packageFee'))
             + _nonnegative(rate.get('warehouseCost')) + freight_station * MoneyRatio(quote))
    vat = _nonnegative(rate.get('vatRate')) * MoneyRatio(Decimal('0.01'))
    fixed = fixed * (MoneyRatio(Decimal(1)) + vat)
    normal_rates = _sum(_nonnegative(rate.get(k)) for k in ('procedureRate', 'commissionRate', 'returnRate', 'otherRate'))
    normal_factor = MoneyRatio(Decimal(1)) - normal_rates * MoneyRatio(Decimal('0.01'))
    full_factor = normal_factor - (_nonnegative(rate.get('marketingRate'))
                                  + _nonnegative(rate.get('publicCommissionRate'))) * MoneyRatio(Decimal('0.01'))
    version = rate['version']
    if not isinstance(version, str) or not version.strip():
        raise ValueError('固定费率缺少版本')
    model = FeeModel(revenue_factor=normal_factor.decimal(), fixed_cost=fixed.decimal(),
                    fixed_cost_divisor=quote, version=version, verified=True)
    online = FeeModel(revenue_factor=full_factor.decimal(), fixed_cost=fixed.decimal(),
                     fixed_cost_divisor=quote, version=version, verified=True)
    return {'online_model': online,
        'current_margin': (model.profit_exact(MoneyRatio(actual)) / MoneyRatio(actual)).decimal(),
        'proof': {'version': version, 'mode': 'direct', 'source_cells': rate.get('source_cells'),
                  'fixed_rate_ref': rate.get('source_ref'), 'weight_g': str(weight),
                  'freight_station': str(freight_station.decimal()), 'fixed_cost_currency': 'CNY',
                  'company_quote': str(quote), 'vat_percent': str(rate['vatRate']),
                  'public_commission_percent': str(rate['publicCommissionRate'])}}
