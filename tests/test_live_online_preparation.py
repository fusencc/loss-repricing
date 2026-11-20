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
    rate={'siteCode':'PH','authType':2,'departmentName':'D1','blocName':'GROUP','totalRate':'35.0411','totalCost':'1.4503',
        'costOfCapital':'2','procedureRate':'2.24','platformRate':'12.8','returnRate':'8','warehouseStorageRate':'0.0001',
        'marketingRate':'10','otherRate':'0.001','packageFee':'0.0001','warehouseCost':'0.7','warehouseCostDomestic':'0.0001',
        'overseaPackageFee':'0.2','publishFee':'0.0001','dischargeFee':'0.35','orderProcessFee':'0.2'}
    sales={'schema_version':'sales-source-observations-v1','source_id':'SALES',**flags,
        'overseas_rates':[rate],'issues':[],'currency_rate_unit':'cny_per_currency',
        'currency_rates':[{'name':'CNY','rate':'1'},{'name':'PHP','rate':'0.1042'}]}
    platform={'schema_version':'sales-platform-controls-v1','source_id':'PLATFORM',**flags,'records':[{
        'erp_shop':'SHOPPH','product_id':'P1','observed_at':flags['completed_at'],'ref':'PLATFORM/1',
        'company_parent':{'shopName':'SHOPPH','shopId':'SHOP-ID','productId':'P1','bloc':'GROUP','department':'D1','localMainSku':'M1','productStatus':4},
        'company_activity_locked':True,'current_variants':[variant],
        'data':{'id':'P1','product_status':'ACTIVATE','status':'ACTIVATE','is_not_for_sale':False,'skus':[{
            'id':'I1','seller_sku':'S1','price':{'sale_price':'61.49','currency':'PHP'},'status_info':{'status':'NORMAL'}}]}}]}
    window={'schema_version':'erp-goods-sales-reference-v1','source_id':'WINDOW',**flags,
        'basis':'erp_goods_warehouse_seven_days_reference','records':{'S1':{'sku':'S1','status':'ok','observed_at':flags['completed_at'],'warehouses':[{'storage_name':'WH-PH','quantity':0,'status':'ok'}]}}}
    local={'schema_version':'local-live-price-history-v1','source_id':'LOCAL',**flags,
        'acquisition_complete':True,'archive_binding':'injected_synthetic_test','executions':[],'guards':[],'protections':[]}
    rate['vatRate']='0'
    fixed={'schema_version':'sales-current-pricing-fees-v1','source_id':'FIXED',**flags,'acquisition_complete':True,'artifact_file':'synthetic-fees.json',
        'config':{'version':'fixed-test-v1','overseas':{'PH':rate},'public':{},'departments':{},'freight':{}}}
    return capture,[target],{'sales':sales,'platform':platform,'window':window,
        'local_history':local,'fixed_fees':fixed,'official_history':{
            'schema_version':'official-recent-price-v1','basis':'official_type_2_completion_time','synthetic':True,
            'source_id':'OFFICIAL','artifact_file':'synthetic.json','completed_at':flags['completed_at'],
            'window_start':'2026-11-08T02:00:30+00:00','window_end':'2026-11-11T02:00:30+00:00',
            'records':{'I1':{'status':'ok','count':0},'I2':{'status':'ok','count':0}}}}


def test_live_online_uses_actual_platform_price_explicit_activity_exact_fee_and_verified_zero_window():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources();r=prepare_online_states(c,t,**s)
    assert not r['issues'] and len(r['online'])==1
    state=r['online'][0]['state']
    assert state['activity_locked'] is True and state['sellable'] is True
    assert state['history_known'] and state['sales_complete'] and state['sales_7d']==0
    assert state['protected'] is False and state['pending_submission'] is False
    assert Decimal(state['current_margin'])<0 and state['profit_check']['model']['fixed_cost_divisor']=='0.1042'
    assert r['contracts'][0]['contract']['rounding']=='ROUND_UP'
    assert r['contracts'][0]['contract']['price_step']=='0.01'
    assert r['contracts'][0]['contract']['upload_column']=='税后价格'
    assert r['pricing_ready'] is False


@pytest.mark.parametrize('case',['price_changed','wrong_main','missing_protection_archive'])
def test_unknown_live_input_never_becomes_optimistic_online_state(case):
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources()
    if case=='price_changed':s['platform']['records'][0]['data']['skus'][0]['price']['sale_price']='62'
    elif case=='wrong_main':s['platform']['records'][0]['company_parent']['localMainSku']='OTHER'
    else:s['local_history']['acquisition_complete']=False
    r=prepare_online_states(c,t,**s)
    assert not r['online'] and r['issues']




def test_foreign_sample_source_cannot_be_joined_to_live_bundle():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources();s['platform']['sample_only']=True
    with pytest.raises(ValueError):prepare_online_states(c,t,**s)


def test_missing_reference_sales_does_not_block_pricing_preparation():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources();s['window']['records']={}
    r=prepare_online_states(c,t,**s)
    assert len(r['online'])==1 and not r['issues']
    assert r['online'][0]['state']['sales_7d'] is None


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
    assert state['last_increase_at'] is None
    assert state['recent_price_log']['count']==0


def test_released_local_guard_does_not_replace_official_history():
    from app.adapters.live_online_preparation import prepare_online_states
    from app.domain.types import VariantKey
    c,t,s=sources();key=VariantKey.model_validate(t[0]['key'])
    s['local_history']['guards']=[{'variant_id':key.target_identity(),'execution_id':None,
        'last_effective_at':'2026-11-11T01:30:00+00:00','linked_mode':None}]
    state=prepare_online_states(c,t,**s)['online'][0]['state']
    assert state['history_known'] is True and state['last_increase_at'] is None
    assert state['recent_price_log']['count']==0



def test_confirmed_terminal_failure_is_not_a_permanent_submission_lock():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources()
    s['local_history']['executions']=[{'execution_id':'FAILED','mode':'live','key':t[0]['key'],'state':'failed','evidence':{}}]
    state=prepare_online_states(c,t,**s)['online'][0]['state']
    assert state['history_known'] and not state['pending_submission']






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
    # 1%下限：((1.0557+.0532+1.4503)/.1042)/(.649589-.01)，五折上传向上进档。
    assert decision.ready and decision.upload_price==Decimal('76.81')
    assert Decimal(decision.trace['online_profit']['rounded_margin']) >= Decimal('.01')
    assert decision.target_actual>=Decimal('33.556') and decision.predicted_profit>=0







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
    candidates=[targets[0],conflict,independent]
    if reverse:candidates.reverse()
    result=prepare_online_states(c,candidates,**inputs)
    assert [entry['state']['key']['item_id'] for entry in result['online']]==['I2']
    assert {entry['target']['warehouse'] for entry in result['issues']}=={'WH-PH','OTHER-WH'}
    assert all('冲突' in entry['reason'] for entry in result['issues'])


def test_fixed_fee_ignores_dynamic_personal_and_shop_overrides():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources()
    parent=s["platform"]["records"][0]["company_parent"]
    parent["department"]="D1"
    rate=s["sales"]["overseas_rates"][0]
    rate.update(authType=2,departmentName="D1")
    s["sales"]["overseas_rates"].append({**rate,"authType":5,"totalRate":"99"})
    result=prepare_online_states(c,t,**s)
    assert not result["issues"]
    check=result["online"][0]["state"]["profit_check"]
    assert check["mode"]=="overseas" and check["department"]=="D1"
    assert Decimal(check["model"]["revenue_factor"])==Decimal("0.649589")


def test_no_overseas_inventory_never_uses_overseas_fee_as_direct_formula():
    from app.adapters.live_online_preparation import prepare_online_states
    c,t,s=sources()
    s["platform"]["records"][0]["current_variants"][0]["overseasStockSum"]=0
    result=prepare_online_states(c,t,**s)
    assert not result["online"]
    assert "本轮当前费用未覆盖" in result["issues"][0]["reason"]


def test_th_online_vat_flows_into_profit_model_without_new_source():
    from app.adapters.live_online_preparation import prepare_online_states
    from test_fixed_pricing_fees import direct_source
    c,targets,inputs=sources()
    targets[0]['key']['site']='TH'
    control=inputs['platform']['records'][0]
    parent=control['company_parent'];parent.update(taxVatRate='24.12',tkCategoryIdLeaf='600747')
    variant=control['current_variants'][0]
    variant.update(currency='THB',overseasStockSum=0,localWeight='21',costPrice='2')
    control['data']['skus'][0]['price']['currency']='THB'
    inputs['sales']['currency_rates']=[{'name':'CNY','rate':'1'},{'name':'THB','rate':'0.1'}]
    _,rate=direct_source()
    config=inputs['fixed_fees']['config']
    config['public']={'SHOPPH':{k:rate[k] for k in ('vatRate','procedureRate','commissionRate','warehouseCost')}}
    config['public']['SHOPPH']['vatRate']=None
    config['departments']={'D1':{'TH':{k:rate[k] for k in ('returnRate','otherRate','marketingRate','packageFee','fixedFreight')}}}
    config['freight']={'TH':rate['freight']}
    result=prepare_online_states(c,targets,**inputs)
    assert not result['issues']
    state=result['online'][0]['state']
    assert Decimal(state['profit_check']['model']['fixed_cost'])==Decimal('6.206')
    proof=next(iter(result['references']['proofs'].values()))['fee']['vat_source']
    assert proof['category_id']=='600747'
    assert proof['source_ref']=='PLATFORM/1'
    assert proof['observed_at']==control['observed_at']
    parent['taxVatRate']=None
    result=prepare_online_states(c,targets,**inputs)
    assert not result['online'] and 'VAT' in result['issues'][0]['reason']
