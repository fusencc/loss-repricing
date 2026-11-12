from copy import deepcopy
from datetime import datetime,timezone
import pytest


def case():
    key={'platform':'TikTok','shop_id':'SHOP-ID','site':'PH','product_id':'P1','item_id':'I1','sku':'S1','warehouse':'WH'}
    target={'erp_shop':'SHOPPH','key':key,'main_sku':'M1','erp_sku':'S1'}
    row={'id':'LOG1','type':2,'status':2,'shopName':'SHOPPH','productId':'P1','sellerSkuId':'I1',
        'priceOld':'10','priceNew':'11','priceOldIncludeVat':None,'priceIncludeVat':None,
        'overseaPriceExcludeVat':None,'directPriceExcludeVat':None,'overseaPriceIncludeVat':None,'directPriceIncludeVat':None,
        'completeTime':'2026-11-11 09:00:00'}
    receipt={'requested_log_id':'LOG1','returned_log_id':None,
        'request_body':{'skus':[{'id':'I1','price':{'amount':'11','currency':'PHP'}}]},
        'platform_response':{'code':0,'data':{}},'ref':'RECEIPTS/LOG1'}
    control={'product_id':'P1','erp_shop':'SHOPPH','platform_price_observed_at':'2026-11-11T02:00:00+00:00',
        'variants_observed_at':'2026-11-11T02:00:01+00:00','observed_at':'2026-11-11T02:00:01+00:00',
        'data':{'id':'P1','skus':[{'id':'I1','price':{'tax_exclusive_price':'11','sale_price':'12.10','currency':'PHP'}}]}}
    return target,row,receipt,control


def test_confirmed_increase_uses_actual_observation_not_company_completion():
    from app.adapters.company_price_history import classify_company_event
    t,r,p,c=case();result=classify_company_event(r,p,target=t,control=c)
    assert result['classification']=='confirmed_increase'
    assert result['observation']['state']=='confirmed'
    assert datetime.fromisoformat(result['observation']['first_confirmed_at'].replace('Z','+00:00'))==datetime(2026,11,11,2,tzinfo=timezone.utc)
    assert result['observation']['logged_complete_at']!=result['observation']['first_confirmed_at']


def test_decrease_does_not_create_a_false_increase():
    from app.adapters.company_price_history import classify_company_event
    t,r,p,c=case();r['priceOld']='12'
    assert classify_company_event(r,p,target=t,control=c)['classification']=='non_increase'


@pytest.mark.parametrize('kind',['increase','decrease','availability'])
def test_any_relevant_event_after_current_observation_requires_refresh(kind):
    from app.adapters.company_price_history import classify_company_event
    t,r,p,c=case();r['completeTime']='2026-11-11 10:00:02'
    if kind=='decrease':r['priceOld']='12'
    if kind=='availability':
        r.update(type=5,priceOld=None,priceNew=None)
        p['request_body']={'product_ids':['P1']}
    assert classify_company_event(r,p,target=t,control=c)['classification']=='snapshot_stale'


@pytest.mark.parametrize('change',['missing_old','currency','duplicate_sku','missing_sku','extra_field','wrong_receipt'])
def test_unproven_receipt_fields_never_confirm_a_price(change):
    from app.adapters.company_price_history import classify_company_event
    t,r,p,c=case()
    if change=='missing_old':r['priceOld']=None
    elif change=='currency':p['request_body']['skus'][0]['price']['currency']='MYR'
    elif change=='duplicate_sku':p['request_body']['skus'].append(deepcopy(p['request_body']['skus'][0]))
    elif change=='missing_sku':p['request_body']['skus'][0]['id']='OTHER'
    elif change=='extra_field':p['request_body']['unknown_price_operation']=True
    else:p['requested_log_id']='OTHER'
    result=classify_company_event(r,p,target=t,control=c)
    assert result['classification']=='unknown' and result['observation'] is None


def test_unmatched_accepted_increase_remains_unverified_and_persistent():
    from app.adapters.company_price_history import classify_company_event
    t,r,p,c=case();c['data']['skus'][0]['price']['tax_exclusive_price']='9'
    result=classify_company_event(r,p,target=t,control=c)
    assert result['classification']=='unverified_increase'
    assert result['observation']['state']=='unverified' and result['observation']['first_confirmed_at'] is None


def test_explicit_availability_only_receipt_is_not_a_price_event():
    from app.adapters.company_price_history import classify_company_event
    t,r,p,c=case();r.update(type=4,priceOld=None,priceNew=None);p['request_body']={'product_ids':['P1']}
    result=classify_company_event(r,p,target=t,control=c)
    assert result['classification']=='availability_only' and result['observation'] is None


def test_multi_sku_receipt_binds_parent_log_to_its_exact_target_amount():
    from app.adapters.company_price_history import classify_company_event
    t,r,p,c=case()
    p['request_body']['skus'].append({'id':'OTHER','price':{'amount':'999','currency':'PHP'}})
    result=classify_company_event(r,p,target=t,control=c)
    assert result['classification']=='confirmed_increase'
    assert result['observation']['new_price']=='11'


def test_multi_sku_parent_for_other_sku_does_not_hide_target_price_change():
    from app.adapters.company_price_history import classify_company_event
    t,r,p,c=case();r['sellerSkuId']='OTHER'
    p['request_body']['skus'].append({'id':'OTHER','price':{'amount':'11','currency':'PHP'}})
    assert classify_company_event(r,p,target=t,control=c)['classification']=='unknown'


def test_accepted_increase_after_snapshot_persists_unverified_observation():
    from app.adapters.company_price_history import classify_company_event
    t,r,p,c=case();r['completeTime']='2026-11-11 10:00:02'
    p['received_at']='2026-11-11T02:00:03+00:00'
    result=classify_company_event(r,p,target=t,control=c)
    assert result['classification']=='snapshot_stale'
    assert result['observation']['state']=='unverified'
    assert result['observation']['first_confirmed_at'] is None
    assert result['observation']['first_observed_at']==p['received_at']
