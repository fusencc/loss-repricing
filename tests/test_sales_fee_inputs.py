"""销售系统费用输入的公式回归；全部为合成数据。"""
from decimal import Decimal, localcontext
import pytest
from app.domain.money import MoneyRatio
from app.adapters.sales_fee_model import compile_direct_fee


def direct_source():
    variant={'currency':'PHP','overseasStockSum':0,'marketPrice':'100','localWeight':'21','costPrice':'2'}
    rate={'siteCode':'PH','version':'fixed-v1','vatRate':'0',
          'procedureRate':'2.24','commissionRate':'8.5','returnRate':'8','otherRate':'0.001',
          'marketingRate':'10','packageFee':'0.3','warehouseCost':'1.3','fixedFreight':'0',
          'freight':{'base_weight_g':'10','base_price':'5','step_price':'4.5','step_weight_g':'10'}}
    return variant,rate


def test_direct_formula_retains_platform_commission_without_product_commission():
    variant,rate=direct_source()
    result=compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')
    # 21g => 首10g 5PHP + 两档4.5PHP = 14PHP；2+.3+1.3+1.4=5CNY。
    assert Decimal(result['proof']['freight_station'])==Decimal('14')
    assert result['online_model'].evaluate(MoneyRatio(Decimal('100'))).profit.decimal()==Decimal('21.259')
    assert 'model' not in result


@pytest.mark.parametrize('weight,freight',[('10','5'),('10.01','9.5'),('20','9.5'),('20.01','14')])
def test_direct_freight_exact_boundaries(weight,freight):
    variant,rate=direct_source();variant['localWeight']=weight
    result=compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')
    assert Decimal(result['proof']['freight_station'])==Decimal(freight)


def test_direct_freight_tier_does_not_depend_on_global_decimal_precision():
    variant,rate=direct_source();variant['localWeight']='20.000000000000000001'
    with localcontext() as context:
        context.prec=3
        result=compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')
    assert Decimal(result['proof']['freight_station'])==Decimal('14')


@pytest.mark.parametrize('change',['missing_weight','missing_cost','nonzero_freight','missing_vat'])
def test_direct_missing_inputs_never_become_zero(change):
    variant,rate=direct_source()
    if change=='missing_weight':variant.pop('localWeight')
    elif change=='missing_cost':variant.pop('costPrice')
    elif change=='nonzero_freight':rate['fixedFreight']='1'
    else:rate['vatRate']=None
    with pytest.raises((ValueError,TypeError,KeyError)):
        compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')


def test_direct_extra_public_commission_is_not_a_formula_input():
    variant,rate=direct_source()
    rate['publicCommissionRate']='5'
    with_field=compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')
    rate.pop('publicCommissionRate')
    without_field=compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')
    assert with_field['online_model']==without_field['online_model']
    assert without_field['online_model'].revenue_factor==Decimal('0.71259')
