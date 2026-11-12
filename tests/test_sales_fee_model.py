import copy
import json
from decimal import Decimal

import pytest


def source():
    variant={'currency':'PHP','marketPrice':'30.745','overseasStockSum':114,
        'currentOverseasProfitMargin':'-14.93',
        'overseasStockWarehouseInfo':json.dumps([{'overseasWarehouses':['WH-PH'],'quantity':114,
            'jqCostpriceList':['1.0557'],'jqFreightList':['0.0532']}])}
    rate={'siteCode':'PH','totalRate':'35.0411','totalCost':'1.4503',
        'costOfCapital':'2','procedureRate':'2.24','platformRate':'12.8','returnRate':'8',
        'warehouseStorageRate':'0.0001','marketingRate':'10','otherRate':'0.001',
        'packageFee':'0.0001','warehouseCost':'0.7','warehouseCostDomestic':'0.0001',
        'overseaPackageFee':'0.2','publishFee':'0.0001','dischargeFee':'0.35','orderProcessFee':'0.2'}
    return variant,rate


def test_overseas_model_reconciles_reported_margin_then_excludes_marketing():
    from app.adapters.sales_fee_model import compile_overseas_fee
    from app.domain.money import MoneyRatio
    v,r=source();result=compile_overseas_fee(v,r,site='PH',warehouse='WH-PH',company_cny_per_currency='0.1042')
    model=result['model']
    assert model.verified and model.revenue_factor==Decimal('0.749589')
    assert model.fixed_cost==Decimal('2.5592') and model.fixed_cost_divisor==Decimal('0.1042')
    assert result['current_margin']<0
    assert result['proof']['reported_margin_percent']=='-14.93'
    assert result['proof']['fixed_cost_currency']=='CNY'
    current=MoneyRatio(Decimal('30.745'))
    target=current+MoneyRatio(Decimal('0.2949'),Decimal('0.1049'))
    assert model.profit_exact(target)>MoneyRatio(Decimal(0))


@pytest.mark.parametrize('change', ['missing_cost','margin_mismatch','wrong_warehouse','stock_ambiguous','wrong_total'])
def test_unknown_or_conflicting_fee_input_is_not_approved(change):
    from app.adapters.sales_fee_model import compile_overseas_fee
    v,r=source()
    if change=='missing_cost':
        buckets=json.loads(v['overseasStockWarehouseInfo']);buckets[0]['jqCostpriceList']=[None]
        v['overseasStockWarehouseInfo']=json.dumps(buckets)
    elif change=='margin_mismatch':v['currentOverseasProfitMargin']='-99'
    elif change=='wrong_warehouse':v['overseasStockWarehouseInfo']=v['overseasStockWarehouseInfo'].replace('WH-PH','OTHER')
    elif change=='stock_ambiguous':
        buckets=json.loads(v['overseasStockWarehouseInfo']);buckets.append(copy.deepcopy(buckets[0]))
        v['overseasStockWarehouseInfo']=json.dumps(buckets)
    else:r['totalCost']='1.45'
    with pytest.raises(ValueError):
        compile_overseas_fee(v,r,site='PH',warehouse='WH-PH',company_cny_per_currency='0.1042')
