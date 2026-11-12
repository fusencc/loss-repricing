import copy
import json
from decimal import Decimal
import pytest


def sources():
    flags={'erp_capture_id':'C1','synthetic':True,'sample_only':False,
        'queried_at':'2026-11-11T02:00:00+00:00','completed_at':'2026-11-11T02:01:00+00:00'}
    capture={'capture_id':'C1','queried_at':'2026-11-11T01:00:00+00:00','synthetic':True,'sample_only':False}
    key={'platform':'TikTok','shop_id':'SHOP-ID','site':'PH','product_id':'P1','item_id':'I1','sku':'S1','warehouse':'WH-PH'}
    target={'erp_shop':'SHOPPH','erp_sku':'S1','main_sku':'M1','key':key}
    variant={'shopName':'SHOPPH','shopId':'SHOP-ID','productId':'P1','sellerSkuId':'I1','sellerSku':'S1','localSellerSku':'S1',
        'currency':'PHP','priceIncludeVat':'61.49','marketPrice':'30.745','overseasStockSum':114,
        'currentOverseasProfitMargin':'-14.93','overseasStockWarehouseInfo':json.dumps([{
            'overseasWarehouses':['WH-PH'],'quantity':114,'jqCostpriceList':['1.0557'],'jqFreightList':['0.0532']}])}
    rate={'siteCode':'PH','authType':1,'blocName':'GROUP','totalRate':'35.0411','totalCost':'1.4503',
        'costOfCapital':'2','procedureRate':'2.24','platformRate':'12.8','returnRate':'8','warehouseStorageRate':'0.0001',
        'marketingRate':'10','otherRate':'0.001','packageFee':'0.0001','warehouseCost':'0.7','warehouseCostDomestic':'0.0001',
        'overseaPackageFee':'0.2','publishFee':'0.0001','dischargeFee':'0.35','orderProcessFee':'0.2'}
    sales={'schema_version':'sales-source-observations-v1','source_id':'SALES',**flags,
        'overseas_rates':[rate],'issues':[],'currency_rate_unit':'cny_per_currency',
        'currency_rates':[{'name':'CNY','rate':'1'},{'name':'PHP','rate':'0.1042'}]}
    platform={'schema_version':'sales-platform-controls-v1','source_id':'PLATFORM',**flags,'records':[{
        'erp_shop':'SHOPPH','product_id':'P1','observed_at':flags['completed_at'],'ref':'PLATFORM/1',
        'company_parent':{'shopName':'SHOPPH','shopId':'SHOP-ID','productId':'P1','bloc':'GROUP','localMainSku':'M1','productStatus':4},
        'company_activity_locked':True,'current_variants':[variant],
        'data':{'id':'P1','product_status':'ACTIVATE','status':'ACTIVATE','is_not_for_sale':False,'skus':[{
            'id':'I1','seller_sku':'S1','price':{'sale_price':'61.49','currency':'PHP'},'status_info':{'status':'NORMAL'}}]}}]}
    window={'schema_version':'erp-complete-sales-window-v1','source_id':'WINDOW',**flags,
        'window_start':'2026-11-04','window_end':'2026-11-10','volumes':[{'erp_shop':'SHOPPH','main_sku':'M1','quantity':0,'complete':True}]}
    company={'schema_version':'sales-company-price-history-v1','source_id':'COMPANY',**flags,
        'company_log_complete':True,'requested_shops':['SHOPPH'],'records':[],
        'queried_at':'2026-11-11T02:02:00+00:00','completed_at':'2026-11-11T02:03:00+00:00',
        'window_start':'2026-11-08T10:02:00+08:00','window_end':'2026-11-11T10:02:00+08:00',
        'protection_scope':'company_log_and_local_verified_archive','platform_manual_policy':'deferred_by_user_20261111'}
    local={'schema_version':'local-live-price-history-v1','source_id':'LOCAL',**flags,
        'acquisition_complete':True,'archive_binding':'injected_synthetic_test','executions':[],'guards':[],'protections':[],
        'company_observations':[]}
    effects={'schema_version':'company-price-effects-v1','source_id':'EFFECTS',**flags,
        'company_history_source_id':'COMPANY','platform_source_id':'PLATFORM','effects':[{
            'key':key,'erp_shop':'SHOPPH','history_known':True,'pending_submission':False,'snapshot_stale':False,
            'confirmed_event_keys':[],'unverified_event_keys':[],'events':[]}],'observations':[]}
    return capture,[target],{'sales':sales,'platform':platform,'window':window,'company_history':company,
        'local_history':local,'company_effects':effects}


def test_live_online_uses_actual_platform_price_explicit_activity_exact_fee_and_verified_zero_window():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources();r=prepare_online_states(c,t,**s)
    assert not r['issues'] and len(r['online'])==1
    state=r['online'][0]['state']
    assert state['activity_locked'] is True and state['sellable'] is True
    assert state['history_known'] and state['sales_complete'] and state['sales_7d']==0
    assert state['protected'] is False and state['pending_submission'] is False
    assert Decimal(state['current_margin'])<0 and state['fee_model']['fixed_cost_divisor']=='0.1042'
    assert r['contracts'][0]['contract']['rounding']=='ROUND_UP'
    assert r['contracts'][0]['contract']['price_step']=='0.01'
    assert r['contracts'][0]['contract']['upload_column']=='税后价格'
    assert r['pricing_ready'] is False


@pytest.mark.parametrize('case',['price_changed','missing_volume','wrong_main','fee_override','missing_protection_archive'])
def test_unknown_live_input_never_becomes_optimistic_online_state(case):
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources()
    if case=='price_changed':s['platform']['records'][0]['data']['skus'][0]['price']['sale_price']='62'
    elif case=='missing_volume':s['window']['volumes']=[]
    elif case=='wrong_main':s['platform']['records'][0]['company_parent']['localMainSku']='OTHER'
    elif case=='fee_override':s['sales']['overseas_rates'].append({**s['sales']['overseas_rates'][0],'authType':5})
    else:s['local_history']['acquisition_complete']=False
    r=prepare_online_states(c,t,**s)
    assert not r['online'] and r['issues']


def test_unclassified_company_log_preserves_unknown_history_instead_of_no_increase():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources();s['company_history']['records']=[{'id':'LOG','shopName':'SHOPPH','productId':'P1','sellerSkuId':'I1','type':2,'status':2}]
    s['company_effects']['effects'][0]['history_known']=False
    r=prepare_online_states(c,t,**s)
    assert r['online'][0]['state']['history_known'] is False


def test_foreign_sample_source_cannot_be_joined_to_live_bundle():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources();s['platform']['sample_only']=True
    with pytest.raises(ValueError):prepare_online_states(c,t,**s)


def test_released_verified_guard_after_72_hours_is_not_a_permanent_submission_lock():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources()
    from app.domain.types import VariantKey
    key=VariantKey.model_validate(t[0]['key']);effective='2026-11-07T01:00:00+00:00'
    s['local_history']['executions']=[{'execution_id':'E1','mode':'live','key':t[0]['key'],'state':'verified',
        'evidence':{'mode':'live','price_basis':'actual_including_vat','actual_price':'30.745','effective_at':effective}}]
    s['local_history']['guards']=[{'variant_id':key.target_identity(),'execution_id':None,'last_effective_at':effective,'linked_mode':None}]
    state=prepare_online_states(c,t,**s)['online'][0]['state']
    assert state['history_known'] and not state['pending_submission']
    from app.adapters.normalization import source_time
    assert source_time(state['last_increase_at'])==source_time(effective)


def test_unlinked_recent_guard_requires_history_proof_instead_of_inventing_effective_price():
    from app.adapters.live_online_preparation import prepare_online_states
    from app.domain.types import VariantKey
    c,t,s=sources();key=VariantKey.model_validate(t[0]['key'])
    s['local_history']['guards']=[{'variant_id':key.target_identity(),'execution_id':None,
        'last_effective_at':'2026-11-11T01:30:00+00:00','linked_mode':None}]
    state=prepare_online_states(c,t,**s)['online'][0]['state']
    assert state['history_known'] is False and state['last_increase_at'] is None



def test_confirmed_terminal_failure_is_not_a_permanent_submission_lock():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources()
    s['local_history']['executions']=[{'execution_id':'FAILED','mode':'live','key':t[0]['key'],'state':'failed','evidence':{}}]
    state=prepare_online_states(c,t,**s)['online'][0]['state']
    assert state['history_known'] and not state['pending_submission']



def test_company_history_ending_before_current_prices_is_unknown():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources()
    s['company_history'].update(window_start='2026-11-08T09:00:00+08:00',window_end='2026-11-11T09:00:00+08:00')
    state=prepare_online_states(c,t,**s)['online'][0]['state']
    assert state['history_known'] is False



def test_user_approved_rounding_runs_the_shared_engine_to_an_archivable_price():
    from app.adapters.live_online_preparation import prepare_online_states
    from app.domain.types import Snapshot,OnlineState,SiteContract,SourceLine
    from app.domain.engine import evaluate
    from app.adapters.normalization import source_time
    c,t,s=sources();s['platform']['records'][0]['company_activity_locked']=False
    bundle=prepare_online_states(c,t,**s);state=OnlineState.model_validate(bundle['online'][0]['state'])
    lines=[SourceLine(row_id='R1',order_id='O1',source=name,key=state.key,main_sku=state.main_sku,quantity=Decimal(1),
        loss_amount=Decimal('-0.2949'),currency='CNY',basis='order',allocation_reliable=True,
        fee_scope=state.fee_scope,abnormal=False,source_ref=name+'/O1')
        for name in ('loss_allocatable','loss_pending','blocked_allocatable','blocked_pending')]
    snapshot=Snapshot(as_of=state.observed_at,business_date='2026-11-11',fx_date='2026-11-11',
        fx_rates={'CNY':'1','PHP':'0.1049'},fx_rate_unit='cny_per_currency',complete_sources=[line.source for line in lines],
        lines=lines,online=[state],contracts={entry['contract']['site']:SiteContract.model_validate(entry['contract'])
            for entry in bundle['contracts']})
    decision=evaluate(snapshot,source_time(bundle['evaluated_at']))[0]
    assert decision.ready and decision.upload_price==Decimal('67.12')
    assert decision.target_actual>=Decimal('33.556') and decision.predicted_profit>=0



def test_persisted_company_confirmation_protects_after_parent_log_leaves_window():
    from app.adapters.live_online_preparation import prepare_online_states
    from app.domain.types import VariantKey
    c,t,s=sources();key=VariantKey.model_validate(t[0]['key'])
    s['local_history']['company_observations']=[{'event_key':'E1','key':t[0]['key'],'state':'confirmed',
        'first_confirmed_at':'2026-11-10T02:00:00+00:00'}]
    state=prepare_online_states(c,t,**s)['online'][0]['state']
    assert state['last_increase_at'] is not None and state['history_known']


def test_unverified_company_observation_stays_pending_when_absent_from_new_query():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources();s['local_history']['company_observations']=[{'event_key':'E1','key':t[0]['key'],
        'state':'unverified','first_confirmed_at':None}]
    state=prepare_online_states(c,t,**s)['online'][0]['state']
    assert state['pending_submission']


@pytest.mark.parametrize('platform_matches',[True,False])
def test_raw_platform_sku_spaces_must_match_without_trimming(platform_matches):
    from app.adapters.live_online_preparation import prepare_online_states
    capture,targets,inputs=sources()
    targets[0]['key']['sku']='S1 '
    record=inputs['platform']['records'][0]
    record['current_variants'][0]['sellerSku']='S1 '
    record['data']['skus'][0]['seller_sku']='S1 ' if platform_matches else 'S1'
    result=prepare_online_states(capture,targets,**inputs)
    assert bool(result['online']) is platform_matches
    if platform_matches:assert result['online'][0]['state']['key']['sku']=='S1 '


@pytest.mark.parametrize('reverse',[False,True])
def test_conflicting_target_warehouses_are_all_held_without_stopping_independent_targets(reverse):
    from app.adapters.live_online_preparation import prepare_online_states
    c,targets,inputs=sources()
    conflict=copy.deepcopy(targets[0]);conflict['key']['warehouse']='OTHER-WH'
    independent=copy.deepcopy(targets[0]);independent['key']['item_id']='I2'
    control=inputs['platform']['records'][0]
    variant=copy.deepcopy(control['current_variants'][0]);variant['sellerSkuId']='I2';control['current_variants'].append(variant)
    sku=copy.deepcopy(control['data']['skus'][0]);sku['id']='I2';control['data']['skus'].append(sku)
    effect=copy.deepcopy(inputs['company_effects']['effects'][0]);effect['key']=independent['key'];inputs['company_effects']['effects'].append(effect)
    candidates=[targets[0],conflict,independent]
    if reverse:candidates.reverse()
    result=prepare_online_states(c,candidates,**inputs)
    assert [entry['state']['key']['item_id'] for entry in result['online']]==['I2']
    assert {entry['target']['warehouse'] for entry in result['issues']}=={'WH-PH','OTHER-WH'}
    assert all('冲突' in entry['reason'] for entry in result['issues'])
