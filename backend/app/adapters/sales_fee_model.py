"""将仓配、库存、成本及费率来源转换为定价模型，并保留计算证据。

当前费率由 current_pricing_fees 绑定本轮版本，销售系统物流模板提供直发运费档位。
ERP 订单修正一次毛利来自财务导出。此处构建新售价的完整在线利润模型。
"""
from decimal import Decimal
import json

from app.adapters.normalization import money
from app.domain.engine import SITE_CURRENCIES
from app.domain.fee_calculation import direct_freight, online_fee_model
from app.domain.money import MoneyRatio

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
    vat = _nonnegative(rate.get('vatRate'))
    version = rate.get('version')
    if not isinstance(version, str) or not version.strip():
        raise ValueError('固定费率缺少版本')
    included_model = online_fee_model(total_rate_percent=total, fixed_cny=fixed, vat_percent=vat,
                                      cny_per_currency=quote, version=version)
    included_percent = included_model.profit_exact(MoneyRatio(actual)) / MoneyRatio(actual) * MoneyRatio(Decimal(100))
    # TODO(T-03)：列表利润率可能采用个人或店铺覆盖费率，待核实两者口径。当前显示值仅作对账。
    reported = variant.get('currentOverseasProfitMargin')
    return {'online_model':included_model,
        'proof':{'version':version,'fixed_cost_currency':'CNY','company_quote_unit':'CNY/'+variant['currency'],
            'company_quote':format(quote,'f'),'fixed_cost':format(fixed.numerator,'f'),
            'reported_margin_percent':None if reported is None else str(reported),
            'online_profit_includes_marketing':True,
            'recalculated_including_marketing_percent':str(included_percent.decimal()),
            'vat_percent':str(rate['vatRate']),
            'rate_source':rate.get('rate_source'),
            'fee_source_ref':rate.get('source_ref'),
            'warehouse':warehouse}}


def compile_direct_fee(variant, rate, *, site, company_cny_per_currency):
    """销售系统首续重物流：g重量分档，跨境运费先按站点币计，再折为CNY成本。

    TH类目VAT复用当前在线父商品字段；SG缺公共费率即关闭，不猜税率或运费。
    公式没有使用额外固定运费；配置非0时要求先明确公式，不能悄悄丢掉。
    """
    if site not in SITE_CURRENCIES or variant.get('currency') != SITE_CURRENCIES[site]:
        raise ValueError('直发币种与站点不一致')
    # 缺失按用户2026-11-20确认选直发；保留原值，告警由规范快照进入计算记录。
    stock = variant.get('overseasStockSum')
    if stock not in (None, '') and money(stock) != 0:
        raise ValueError('有海外库存不能使用直发公式')
    quote = _positive(company_cny_per_currency)
    weight = _positive(variant.get('localWeight'))
    freight = rate['freight']
    base_weight = _positive(freight['base_weight_g'])
    freight_station = direct_freight(weight, base_weight, _nonnegative(freight['base_price']),
                                     _nonnegative(freight['step_price']), _positive(freight['step_weight_g']))
    if money(rate.get('fixedFreight')) != 0:
        # TODO(T-04)：待确认非零固定运费在直发公式中的位置后，再支持这类配置。
        raise ValueError('固定运费非0，须确认其在直发公式中的位置')
    fixed = (_nonnegative(variant.get('costPrice')) + _nonnegative(rate.get('packageFee'))
             + _nonnegative(rate.get('warehouseCost')) + freight_station * MoneyRatio(quote))
    vat = _nonnegative(rate.get('vatRate'))
    # 完整在线利润包含营销费与基础平台佣金；不叠加公开计划佣金。
    rates = _sum(_nonnegative(rate.get(k)) for k in
        ('procedureRate', 'commissionRate', 'returnRate', 'otherRate', 'marketingRate'))
    version = rate['version']
    if not isinstance(version, str) or not version.strip():
        raise ValueError('固定费率缺少版本')
    online = online_fee_model(total_rate_percent=rates, fixed_cny=fixed, vat_percent=vat,
                              cny_per_currency=quote, version=version)
    return {'online_model': online,
        'proof': {'version': version, 'mode': 'direct',
                  'logistics_source': rate.get('logistics_source'), 'department_source': rate.get('department_source'),
                  'fee_source_ref': rate.get('source_ref'), 'weight_g': str(weight),
                  'freight_station': str(freight_station.decimal()), 'fixed_cost_currency': 'CNY',
                  'company_quote': str(quote), 'vat_percent': str(rate['vatRate']),
                  'rate_source': rate.get('rate_source')}}
