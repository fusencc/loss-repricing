from datetime import timedelta
from decimal import Decimal
import pytest
from test_execution_safety import safety_context


def test_web_seven_day_observation_must_be_from_current_business_day():
    from test_pricing import single, NOW
    result = single(online_fields={'sales_basis': 'sales_system_web_sku_seven_days',
                                   'sales_observed_at': NOW - timedelta(days=1)})
    assert result.reason_code == 'invalid_sales'


def test_local_history_does_not_depend_on_external_company_log():
    from test_live_online_preparation import sources
    from app.adapters.live_online_preparation import prepare_online_states
    capture, targets, sources_ = sources()
    sources_['local_history']['company_observations'] = [
        {'key': targets[0]['key'], 'state':'unverified', 'first_confirmed_at':None}]
    state = prepare_online_states(capture, targets, **sources_)['online'][0]['state']
    assert state['history_known'] is True
    assert state['pending_submission'] is False


def test_fixed_overseas_formula_includes_vat_and_public_commission():
    from test_sales_fee_model import source
    from app.adapters.sales_fee_model import compile_overseas_fee
    variant, rate = source()
    rate.update(vatRate='10', publicCommissionRate='5', version='fixed-test-v1')
    result = compile_overseas_fee(variant, rate, site='PH', warehouse='WH-PH', company_cny_per_currency='0.1')
    # 2.5592 CNY × 1.1 / 0.1 = 28.1512 PHP；a = 1 - 35.0411% - 5%。
    assert result['online_model'].profit(Decimal('100')) == Decimal('31.8077')
    # 前置一次毛利仍沿用排除营销费用的已有口径。
    assert result['model'].revenue_factor == Decimal('0.749589')


def test_live_enrichment_uses_own_database_even_if_external_snapshot_claims_increase(safety_context):
    from app.services.execution import PricingService
    factory,cfg,adapter,clock=safety_context
    adapter.mode='live'
    snapshot=adapter.collect(clock[0].date())
    for row in snapshot.online:
        row.history_known=False
        row.last_increase_at=clock[0]-timedelta(hours=1)
        row.pending_submission=True
    result=PricingService(factory,adapter,cfg)._enrich(snapshot)
    assert all(row.history_known and not row.pending_submission and row.last_increase_at is None for row in result.online)


@pytest.mark.parametrize('state,mode,actual,known,pending',[
    ('verified','live','11',True,False),('unknown','live','11',True,True),
    ('verified','demo','11',True,False),('failed','live','11',True,False),
    ('verified','live','Infinity',False,False),('verified','live','NaN',False,False)])
def test_only_verified_live_effect_is_a_local_increase(safety_context,state,mode,actual,known,pending):
    from app.models import Batch,Item,Execution
    from app.services.execution import PricingService
    factory,cfg,adapter,clock=safety_context;adapter.mode='live'
    snapshot=adapter.collect(clock[0].date());key=snapshot.online[0].key
    effective=clock[0]-timedelta(hours=1);stamp=effective.isoformat()
    with factory.begin() as session:
        session.add(Batch(id='B',run_key='B',business_date=clock[0].date().isoformat(),mode=mode,
            status='done',created_at=stamp,snapshot={},summary={}))
        session.flush()
        session.add(Item(id='I',batch_id='B',variant_id=key.target_identity(),key=key.model_dump(mode='json'),
            status='待处理',phase=state,reason='',decision={},updated_at=stamp))
        session.flush()
        session.add(Execution(id='E',item_id='I',variant_id=key.target_identity(),state=state,request={},response={},
            evidence={'mode':mode,'effective_at':stamp,'actual_price':actual,'price_basis':'actual_including_vat'},
            created_at=stamp,updated_at=stamp))
    result=PricingService(factory,adapter,cfg)._enrich(snapshot).online[0]
    assert result.history_known is known and result.pending_submission is pending
    assert result.last_increase_at == (effective if mode=='live' and state=='verified' and known else None)
