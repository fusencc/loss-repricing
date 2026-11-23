"""计算层重构的独立手算回归；全部使用合成数据，不访问真实服务。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from app.domain.engine import fx_ratio
from app.domain.fee_calculation import direct_freight, online_fee_model
from app.domain.money import MoneyRatio
from app.domain.types import FeeModel, OnlineProfitCheck, ProfitCheckIssue
from test_pricing import snapshot


@pytest.mark.parametrize('unit,from_rate,to_rate,numerator,denominator', [
    ('cny_per_currency', '1', '3', '1', '3'),
    ('currency_per_cny', '3', '1', '1', '3'),
    ('cny_per_currency', '3', '1', '3', '1'),
    ('currency_per_cny', '1', '3', '3', '1'),
])
def test_fx_directions_preserve_exact_fraction(unit, from_rate, to_rate, numerator, denominator):
    batch = snapshot([], [], fx_rates={'CNY': '1', 'USD': from_rate, 'MYR': to_rate}, fx_rate_unit=unit)
    with localcontext() as context:
        context.prec = 6
        result = fx_ratio(batch, 'USD', 'MYR')
        assert result == MoneyRatio(Decimal(numerator), Decimal(denominator))
        three_units = result * MoneyRatio(Decimal(3))
        assert three_units.round_to_step(Decimal(1), 'ROUND_UP') == Decimal(numerator) * 3 / Decimal(denominator)


@pytest.mark.parametrize('unit', ['cny_per_currency', 'currency_per_cny'])
def test_same_currency_has_no_conversion(unit):
    batch = snapshot([], [], fx_rates={'CNY': '1', 'MYR': '3'}, fx_rate_unit=unit)
    assert fx_ratio(batch, 'MYR', 'MYR') == MoneyRatio(Decimal(1))


@pytest.mark.parametrize('weight,expected', [('9.9', '5'), ('10', '5'), ('10.01', '9.5'),
                                            ('20', '9.5'), ('20.01', '14')])
def test_freight_weight_boundaries(weight, expected):
    assert direct_freight(Decimal(weight), Decimal(10), MoneyRatio(Decimal(5)),
                          MoneyRatio(Decimal('4.5')), Decimal(10)) == MoneyRatio(Decimal(expected))


def test_online_model_vat_quote_and_profit_are_hand_calculated():
    # 费率20%，固定成本10CNY，VAT10%，报价3CNY/站点币：G(10)=8-11/3=13/3。
    model = online_fee_model(total_rate_percent=MoneyRatio(Decimal(20)),
                             fixed_cny=MoneyRatio(Decimal(10)), vat_percent=MoneyRatio(Decimal(10)),
                             cny_per_currency=Decimal(3), version='synthetic-v1')
    assert model.revenue_factor == Decimal('0.8')
    assert model.fixed_cost == Decimal(11)
    assert model.fixed_cost_divisor == Decimal(3)
    assert model.profit_exact(MoneyRatio(Decimal(10))) == MoneyRatio(Decimal(13), Decimal(3))


def test_profit_validation_order_and_existing_messages():
    now = datetime(2026, 11, 23, 2, tzinfo=timezone.utc)
    check = OnlineProfitCheck(overseas_stock=Decimal(-1), mode='direct', department='',
                              inventory_observed_at=now, source_ref='',
                              model=FeeModel(revenue_factor=Decimal('0.8'), fixed_cost=Decimal(1), version='v1'))
    assert check.validation_issue(now, now.date()) is ProfitCheckIssue.STOCK_MODE
    check.overseas_stock = None
    assert check.validation_issue(now, now.date()) is ProfitCheckIssue.SOURCE
    check.department, check.source_ref = 'D1', 'synthetic'
    assert check.validation_issue(now, now.date()) is ProfitCheckIssue.MODEL
    assert ProfitCheckIssue.MODEL.value == '在线利润模型未确认或不能计算在线利润校正额'
    check.model.verified = True
    assert check.validation_issue(now, now.date()) is None
    check.inventory_observed_at = now - timedelta(days=1)
    assert check.validation_issue(now, now.date()) is ProfitCheckIssue.SOURCE


def test_reference_can_waive_minimum_margin_but_not_model_confirmation():
    now = datetime(2026, 11, 23, 2, tzinfo=timezone.utc)
    check = OnlineProfitCheck(mode='direct', department='D1', inventory_observed_at=now,
                              source_ref='synthetic', model=FeeModel(revenue_factor=Decimal('0.01'),
                              fixed_cost=Decimal(1), version='v1', verified=True))
    assert check.validation_issue(now, now.date()) is ProfitCheckIssue.MODEL
    assert check.validation_issue(now, now.date(), minimum_margin_required=False) is None
    check.model.verified = False
    assert check.validation_issue(now, now.date(), minimum_margin_required=False) is ProfitCheckIssue.MODEL


@pytest.mark.parametrize('factor,fixed_cny,expected', [
    ('0.8', '10', MoneyRatio(Decimal(1000), Decimal(237))),
    ('0.01', '0', MoneyRatio(Decimal(0))),
    ('0.01', '10', None),
    ('0.009', '0', None),
])
def test_minimum_price_solves_exactly_or_reports_no_finite_floor(factor, fixed_cny, expected):
    from test_online_profit_check import check

    evidence = check(factor=factor, cost=fixed_cny)
    evidence.model.fixed_cost_divisor = Decimal(3)
    assert evidence.minimum_actual_price() == expected


@pytest.mark.parametrize('loss_currency,quote', [('MYR', None), ('MYR', '0'), ('USD', '-1')])
def test_invalid_quote_precedes_current_price_even_when_currency_matches(loss_currency, quote):
    from test_pricing import single

    rates = {'CNY': '1', 'MYR': '3'}
    if quote is None:
        rates.pop(loss_currency)
    else:
        rates[loss_currency] = quote
    result = single(line_fields={'currency': loss_currency}, online_fields={'retail_price': '0'},
                    snapshot_fields={'fx_rates': rates})
    assert result.reason_code == 'invalid_fx'


@pytest.mark.parametrize('marketing,expected', [
    (None, (Decimal(3), MoneyRatio(Decimal(1)))),
    ('1', (Decimal(1), MoneyRatio(Decimal(1), Decimal(3)))),
    ('0', None),
    ('3.01', None),
])
def test_observed_discount_distinguishes_missing_and_invalid_marketing_prices(marketing, expected):
    from test_pricing import key, online

    state = online(key(), retail_price='3', marketing_price=marketing)
    assert state.actual_price_and_discount() == expected
