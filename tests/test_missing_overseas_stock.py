"""2026-11-20：缺库存用直发并告警；合成来源，不访问真实接口。"""
from decimal import Decimal

import pytest

from app.adapters.erp_order_loss import _warehouse_matches
from app.adapters.live_online_preparation import prepare_online_states
from app.domain.types import OnlineProfitCheck
from test_sales_fee_inputs import direct_source
from test_live_online_preparation import sources, configure_direct
from test_online_profit_check import check
from test_pricing import NOW, single

WARNING = '海外库存缺失，已按直发模型计算，请核实库存'


@pytest.mark.parametrize('stock', [None, '', 'absent'])
def test_missing_stock_prepares_direct_model_without_fabricating_zero(stock):
    capture, targets, inputs = sources()
    variant = inputs['platform']['records'][0]['current_variants'][0]
    variant.update(localWeight='21', costPrice='2')
    if stock == 'absent':
        variant.pop('overseasStockSum')
    else:
        variant['overseasStockSum'] = stock
    _, rate = direct_source()
    config = inputs['fixed_fees']['config']
    configure_direct(config,rate,'PH')
    result = prepare_online_states(capture, targets, **inputs)
    assert not result['issues'] and len(result['online']) == 1
    evidence = result['online'][0]['state']['profit_check']
    assert evidence['overseas_stock'] is None and evidence['mode'] == 'direct'
    assert OnlineProfitCheck.model_validate(evidence).warnings() == [WARNING]


def test_missing_stock_computes_and_persists_warning():
    result = single(online_fields={'profit_check': check(stock=None)})
    assert result.ready
    assert result.trace['online_profit']['overseas_stock'] is None
    assert result.trace['warnings'] == [WARNING]
    assert result.upload_price == Decimal('12.66')


@pytest.mark.parametrize('stock,mode', [('0', 'direct'), ('2', 'overseas')])
def test_known_inventory_does_not_warn(stock, mode):
    result = single(online_fields={'profit_check': check(stock=stock, mode=mode)})
    assert result.ready and result.trace['warnings'] == []


@pytest.mark.parametrize('stock,mode', [('-1', 'direct'), (None, 'overseas')])
def test_negative_or_conflicting_inventory_still_blocks(stock, mode):
    result = single(online_fields={'profit_check': check(stock=stock, mode=mode)})
    assert not result.ready and result.reason_code == 'unverified_online_profit'


def test_missing_stock_still_requires_warehouse_ownership():
    variant = {'warehousename': 'DIRECT'}
    assert _warehouse_matches(variant, 'DIRECT')
    assert not _warehouse_matches(variant, 'OTHER')
    assert not _warehouse_matches({}, 'DIRECT')


def test_manual_correction_keeps_missing_stock_warning():
    from app.services.corrections import manual_decision
    from test_pricing import key, online, line, snapshot
    k = key()
    state = online(k, profit_check=check(stock=None))
    result = manual_decision(snapshot([line(k)], [state]), state, Decimal('12.66'),
                             'synthetic', 'C1', NOW)
    assert result.ready and result.trace['warnings'] == [WARNING]
