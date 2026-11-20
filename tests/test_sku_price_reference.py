"""同SKU营销均价及价格上限；有参考时优先限价，缺参考沿用1%利润。"""
from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest


def sample(shop, quantity, retail, discount):
    from app.domain.price_reference import PriceReferenceSample
    return PriceReferenceSample(shop_name=shop, product_id='P-' + shop,
        item_id='I-' + shop, sales_7d=quantity,
        tax_inclusive_price=Decimal(retail), discount_percent=Decimal(discount))


def reference(rows):
    from app.domain.price_reference import calculate_price_reference
    return calculate_price_reference(rows, sku='S1', currency='PHP',
        observed_at=datetime(2026, 11, 20, tzinfo=timezone.utc), source_ref='source/1')


def test_top3_is_sales_ranked_arithmetic_mean_using_tax_price_and_discount():
    rows = [sample('A', 8, '100', '50'), sample('B', 7, '80', '25'),
            sample('C', 6, '100', '0'), sample('D', 1, '999', '0')]
    result = reference(rows)
    # (50 + 60 + 100) / 3 = 70；不取中位数60，不按销量加权。
    assert result.marketing_price == Decimal('70')
    assert [r.shop_name for r in result.samples] == ['A', 'B', 'C']
    assert result.samples[2].marketing_price == Decimal('100')


def test_formula_preserves_fractional_cent_and_ignores_decimal_context():
    with localcontext() as context:
        context.prec = 6
        result = reference([sample(s, 1, '91.35', '50') for s in 'ABC'])
    assert result.marketing_price == Decimal('45.675')
    assert result.model_dump(mode='json')['samples'][0]['marketing_price'] == '45.675'


@pytest.mark.parametrize('quantities', [[], [3], [3, 2], [3, 2, 0]])
def test_less_than_three_other_selling_links_has_no_reference(quantities):
    assert reference([sample(str(i), q, '100', '0') for i, q in enumerate(quantities)]) is None


def test_duplicate_link_cannot_fill_top3():
    a = sample('A', 3, '100', '0')
    assert reference([a, a, sample('B', 2, '100', '0')]) is None


def test_two_specs_on_the_same_other_product_are_not_two_comparable_links():
    a=sample('A',3,'100','0')
    alias=a.model_copy(update={'item_id':'OTHER-SPEC'})
    assert reference([a,alias,sample('B',2,'100','0')]) is None


def test_formula_does_not_use_a_displayed_marketing_price():
    # 参考必须按用户指定公式，不跟随系统营销价的分币舍入45.68。
    result = reference([sample(s, 1, '91.35', '50') for s in 'ABC'])
    assert result.marketing_price != Decimal('45.68')


def ceiling(amount, currency='MYR'):
    return reference([sample(s,1,amount,'0') for s in 'ABC']).model_copy(update={'currency':currency})


def test_reference_caps_the_target_marketing_price():
    from test_pricing import single
    result=single(online_fields={'sku_price_reference':ceiling('10.50')})
    assert result.ready
    assert result.target_actual==Decimal('10.50') and result.upload_price==Decimal('10.50')
    assert result.trace['price_limit']['applied'] is True
    assert Decimal(result.trace['price_limit']['reduction_amount'])==Decimal('.7')
    assert Decimal(result.trace['online_profit']['correction_amount'])==0


def test_price_cap_below_one_percent_floor_still_adjusts_to_reference():
    from test_pricing import single
    from test_online_profit_check import check
    result=single(online_fields={'sku_price_reference':ceiling('12.5'),'profit_check':check()})
    assert result.ready and result.reason_code=='ready'
    assert result.target_actual==result.upload_price==Decimal('12.5')
    assert result.predicted_profit==0
    assert result.trace['online_profit']['minimum_margin_required'] is False
    assert Decimal(result.trace['online_profit']['rounded_margin'])==0
    assert 'blocked_reason' not in result.trace['price_limit']


@pytest.mark.parametrize('amount',['9','10'])
def test_current_price_at_or_above_cap_is_adjusted_to_reference(amount):
    from test_pricing import single
    result=single(online_fields={'sku_price_reference':ceiling(amount)})
    assert result.ready and result.reason_code=='ready'
    assert result.target_actual==result.upload_price==Decimal(amount)
    assert result.current_actual==Decimal('10')


@pytest.mark.parametrize('factor',['0','.01','-.1'])
def test_unattainable_profit_floor_uses_reference_without_division_or_profit_veto(factor):
    from test_pricing import single
    from test_online_profit_check import check
    result=single(online_fields={'sku_price_reference':ceiling('12.5'),
                                'profit_check':check(factor=factor)})
    assert result.ready and result.upload_price==Decimal('12.5')
    assert result.predicted_profit==Decimal('12.5')*Decimal(factor)-Decimal('10')
    assert result.trace['online_profit']['break_even'] is None
    assert result.trace['online_profit']['minimum_margin_required'] is False


def test_reference_does_not_remove_source_model_validation():
    from test_pricing import single
    from test_online_profit_check import check
    evidence=check()
    evidence.model.verified=False
    result=single(online_fields={'sku_price_reference':ceiling('9'),'profit_check':evidence})
    assert not result.ready and result.reason_code=='unverified_online_profit'


@pytest.mark.parametrize('ref',[None,ceiling('100'),ceiling('1','PHP')])
def test_missing_high_or_different_currency_reference_preserves_original_price(ref):
    from test_pricing import single
    base=single();result=single(online_fields={'sku_price_reference':ref})
    assert result.ready and result.upload_price==base.upload_price
    assert result.target_actual==base.target_actual and result.predicted_profit==base.predicted_profit


def test_upload_rounding_cannot_exceed_reference_even_by_a_fractional_cent():
    from test_pricing import single,contract
    result=single(line_fields={'loss_amount':'-1.204'},
        online_fields={'retail_price':'20','marketing_price':'10','sku_price_reference':ceiling('11.204')},
        snapshot_fields={'contracts':{'MY':contract(rounding='ROUND_UP')}})
    assert result.ready and result.upload_price==Decimal('22.40')
    assert result.target_actual==Decimal('11.2')
    assert Decimal(result.trace['rounded_actual'])<=Decimal('11.204')


def test_cap_uses_exact_mean_before_upload_grid_rounding():
    from test_pricing import single,contract
    ref=reference([sample('A',3,'10','0'),sample('B',2,'10','0'),sample('C',1,'11','0')])
    ref=ref.model_copy(update={'currency':'MYR'})
    result=single(line_fields={'loss_amount':'-20'},
        online_fields={'retail_price':'3','marketing_price':'1','sku_price_reference':ref},
        snapshot_fields={'contracts':{'MY':contract(rounding='ROUND_UP')}})
    # 均价31/3，折后比例1/3，最高上传价恰为31；不能因循环小数少进一档到30.99。
    assert result.ready and result.upload_price==Decimal('31.00')


def test_reference_below_one_upload_tick_does_not_abort_other_variants():
    from test_pricing import snapshot,line,key,online,NOW
    from app.domain.engine import evaluate
    a=key();b=key(item_id='0002',sku='B')
    results=evaluate(snapshot([line(a),line(b,row_id='r2',order_id='o2')],
        [online(a,sku_price_reference=ceiling('.005')),online(b)]),NOW)
    first,second=results
    assert not first.ready and first.reason_code=='rounding_error'
    assert first.trace['online_profit']['target_margin'] is None
    assert second.ready and second.upload_price==Decimal('11.2')


def test_manual_explicit_price_above_reference_is_rejected_without_rewriting_authorization():
    from test_pricing import online,key,line,snapshot,NOW
    from app.services.corrections import manual_decision
    state=online(key(),sku_price_reference=ceiling('11'))
    with pytest.raises(ValueError,match='参考上限'):
        manual_decision(snapshot([line(key())],[state]),state,Decimal('11.2'),'test','C',NOW)


def test_manual_rounding_also_must_stay_under_the_reference():
    from test_pricing import online,key,line,snapshot,NOW,contract
    from app.services.corrections import manual_decision
    state=online(key(),retail_price='20',marketing_price='10',sku_price_reference=ceiling('11.203'))
    snap=snapshot([line(key())],[state],contracts={'MY':contract(rounding='ROUND_UP')})
    with pytest.raises(ValueError,match='参考上限'):
        manual_decision(snap,state,Decimal('11.203'),'test','C',NOW)


@pytest.mark.parametrize('factor',['.8','.01'])
def test_manual_price_with_reference_allows_negative_profit_and_price_decrease(factor):
    from test_pricing import online,key,line,snapshot,NOW
    from test_online_profit_check import check
    from app.services.corrections import manual_decision
    state=online(key(),sku_price_reference=ceiling('9'),profit_check=check(factor=factor))
    result=manual_decision(snapshot([line(key())],[state]),state,Decimal('9'),'test','C',NOW)
    assert result.ready and result.upload_price==Decimal('9')
    assert result.predicted_profit < 0
    assert result.trace['minimum_margin_required'] is False


def test_different_optional_references_are_not_conflicting_online_prices():
    from test_pricing import snapshot, line, key, online, NOW
    from app.domain.engine import evaluate
    ref = reference([sample(s, 1, '100', '0') for s in 'ABC'])
    k = key()
    first = online(k)
    second = online(k, sku_price_reference=ref)
    result = evaluate(snapshot([line(k)], [first, second]), NOW)[0]
    assert result.ready and result.reason_code == 'ready'


@pytest.mark.parametrize('first_currency',['PHP','MYR'])
@pytest.mark.parametrize('reverse',[False,True])
def test_duplicate_online_rows_cannot_bypass_cap_by_currency_or_order(first_currency,reverse):
    from test_pricing import snapshot,line,key,online,NOW
    from app.domain.engine import evaluate
    k=key()
    rows=[online(k,sku_price_reference=ceiling('100',first_currency)),
          online(k,sku_price_reference=ceiling('10.5'))]
    if reverse:rows.reverse()
    result=evaluate(snapshot([line(k)],rows),NOW)[0]
    assert result.ready and result.target_actual==Decimal('10.5')
    assert result.trace['price_limit']['upper_actual']=='10.5'

