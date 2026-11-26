import copy
from decimal import Decimal
import json

import pytest
from app.adapters.erp_pricing import CAPTURE_VERSION


def inputs():
    flags={'synthetic':True,'sample_only':False}
    capture={'capture_id':'C1','source_kind':'erp_web_backend','source_version':CAPTURE_VERSION,
        'acquisition_mode':'bulk_download','capture_complete':True,'queried_at':'2026-11-11T01:00:00+00:00',**flags,
        'groups':[{'source':'loss_allocatable','status':4,'filters':{'platform':'Tiktok','bloc':'GROUP','status':4,'financecheck':'3'},'list_complete':True,'stage':'complete','error':None,'total':1,'batches':[],'candidate_order_ids':['O1'],'rows':[
            {'orderid':'O1','current_status_matches_source':True,'fields':{'订单编号':'O1','订单状态':'可配货','平台':'Tiktok','集团':'GROUP','店铺':'SHOPPH',
             '商品SKU列表':'S1x3[LOCATION]','修正一次毛利':'-3.0','实体仓库名称':'WH-PH','订单属性':'正常订单','item ID':'P1'}}]}]}
    binding={'erp_capture_id':'C1',**flags,'queried_at':'2026-11-11T02:00:00+00:00','completed_at':'2026-11-11T02:01:00+00:00'}
    native={'schema_version':'erp-order-details-v1','source_id':'N1',**binding,
        'requested_order_ids':['O1'],'acquisition_complete':True,
        'batches':[{'order_ids':['O1'],'status':'complete'}],
        'rows':[{'order_id':'O1','ref':'physical-1','fields':{'订单编号':'O1','平台':'Tiktok','店铺':'SHOPPH','商品SKU列表':'S1','订单数量':1}},
                {'order_id':'O1','ref':'physical-2','fields':{'订单编号':'O1','平台':'Tiktok','店铺':'SHOPPH','商品SKU列表':'S1','订单数量':2}}]}
    sales={'schema_version':'sales-source-observations-v1','source_id':'S1',**binding,
        'products':[{'shopName':'SHOPPH','productId':'P1','site':'PH','bloc':'GROUP','localMainSku':'MAIN1','shopId':'SHOP-ID'}],
        'variants':[{'shopName':'SHOPPH','productId':'P1','shopId':'SHOP-ID','sellerSkuId':'I1','sellerSku':'PLATFORM-S1',
            'localSellerSku':'S1','localMainSku':'MAIN1','overseasStockSum':5,
            'overseasStockWarehouseInfo':json.dumps([{'overseasWarehouses':['WH-PH'],'quantity':5}])}]}
    return capture,native,sales


def test_reliable_same_sku_order_uses_order_record_identity_and_all_quantity():
    from app.adapters.erp_order_loss import prepare_order_losses
    capture,native,sales=inputs();original=copy.deepcopy(native)
    result=prepare_order_losses(capture,native,sales)
    assert not result['issues'] and len(result['orders'])==1
    line=result['orders'][0]['lines'][0]
    assert line['detail_id']=='O1'  # 实际订单级金额记录的自然ID，不伪造原生明细serial。
    assert line['loss']['basis']=='order' and line['loss']['allocation_reliable']
    assert Decimal(line['loss']['quantity'])==3 and Decimal(line['loss']['amount'])==-3
    assert line['loss']['currency']=='CNY'
    assert line['mapping']['key']['item_id']=='I1' and line['mapping']['key']['sku']=='PLATFORM-S1'
    proof=result['proofs']['O1']
    assert proof['record_kind']=='same_sku_order' and proof['native_refs']==['physical-1','physical-2']
    assert result['pricing_ready'] is False and native==original


@pytest.mark.parametrize('change,code', [('multi','multi_sku_order'),('quantity','order_quantity_mismatch'),('attribute','unreviewed_order_attribute')])
def test_unreliable_order_is_held_without_allocating(change,code):
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs()
    if change=='multi':n['rows'][1]['fields']['商品SKU列表']='S2'
    elif change=='quantity':c['groups'][0]['rows'][0]['fields']['商品SKU列表']='S1x2[LOCATION]'
    else:c['groups'][0]['rows'][0]['fields']['订单属性']='炒分订单'
    r=prepare_order_losses(c,n,s)
    assert r['orders']==[] and any(i['code']==code for i in r['issues'])


def test_financial_amount_conflict_is_not_resolved_by_taking_first_source():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();g=copy.deepcopy(c['groups'][0]);g['source']='blocked_allocatable'
    g['rows'][0]['fields']['修正一次毛利']='-4';c['groups'].append(g)
    r=prepare_order_losses(c,n,s)
    assert not r['orders'] and any(i['code']=='conflicting_order_finance' for i in r['issues'])


def test_ambiguous_online_target_keeps_loss_proof_without_guessing_target():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();other=copy.deepcopy(s['variants'][0]);other['sellerSkuId']='I2';s['variants'].append(other)
    r=prepare_order_losses(c,n,s)
    assert r['orders'][0]['lines'][0]['mapping'] is None
    assert r['orders'][0]['lines'][0]['loss']['amount']=='-3.0'
    assert any(i['code']=='ambiguous_online_mapping' for i in r['issues'])


def test_sample_or_foreign_capture_cannot_be_joined():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();n['sample_only']=True
    with pytest.raises(ValueError):prepare_order_losses(c,n,s)


def test_current_real_capture_contract_provides_real_flag():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();c.pop('synthetic');n['synthetic']=s['synthetic']=False
    assert prepare_order_losses(c,n,s)['synthetic'] is False


def test_source_clock_skew_within_the_business_day_keeps_verified_orders():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();n['completed_at']='2026-11-11T01:30:00+00:00'
    assert [order['order_id'] for order in prepare_order_losses(c,n,s)['orders']] == ['O1']


def test_null_parent_site_is_not_a_missing_site_when_shop_suffix_is_known():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();s['products'][0]['site']=None
    r=prepare_order_losses(c,n,s)
    assert r['orders'][0]['lines'][0]['mapping']['key']['site']=='PH'


def test_parent_owns_main_sku_when_current_expanded_variant_field_is_null():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();s['products'][0]['localMainSku']='MAIN1';s['variants'][0]['localMainSku']=None
    r=prepare_order_losses(c,n,s)
    assert r['orders'][0]['lines'][0]['mapping']['main_sku']=='MAIN1'


def test_conflicting_parent_and_variant_main_sku_does_not_pick_one():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();s['products'][0]['localMainSku']='OTHER'
    r=prepare_order_losses(c,n,s)
    assert r['orders'][0]['lines'][0]['mapping'] is None


def test_parent_and_variant_shop_ids_must_agree():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();s['products'][0]['shopId']='OTHER'
    assert prepare_order_losses(c,n,s)['orders'][0]['lines'][0]['mapping'] is None


def test_stable_shop_scope_survives_multisku_hold_but_not_conflicting_owner():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();n['rows'][1]['fields']['商品SKU列表']='S2'
    headers={'schema_version':'erp-order-headers-v1','source_id':'H1',
        **{key:n[key] for key in ('erp_capture_id','synthetic','sample_only','queried_at','completed_at')},
        'requested_order_ids':['O1'],'orders':{'O1':{'orderid':'O1','shopid':'SHOP-ID','shopname':'SHOPPH','platform':'Tiktok','ref':'H1/O1'}}}
    result=prepare_order_losses(c,n,s,order_headers=headers)
    assert result['shop_scopes']=={'O1':{'shop_id':'SHOP-ID','erp_shop':'SHOPPH','ref':'H1/O1'}}
    assert not result['orders']
    headers['orders']['O1']['shopname']='OTHER'
    assert not prepare_order_losses(c,n,s,order_headers=headers)['shop_scopes']


def test_stable_shop_header_conflict_rejects_online_mapping():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs()
    headers={'schema_version':'erp-order-headers-v1','source_id':'H1',
        **{key:n[key] for key in ('erp_capture_id','synthetic','sample_only','queried_at','completed_at')},
        'requested_order_ids':['O1'],'orders':{'O1':{'orderid':'O1','shopid':'OTHER-ID','shopname':'SHOPPH','platform':'Tiktok','ref':'H1/O1'}}}
    result=prepare_order_losses(c,n,s,order_headers=headers)
    assert result['orders'][0]['lines'][0]['mapping'] is None


def test_conflicting_amount_still_proves_only_shop_ownership_and_holds_all_its_prices():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();group=copy.deepcopy(c['groups'][0]);group['source']='blocked_allocatable'
    group['rows'][0]['fields']['修正一次毛利']='-4';c['groups'].append(group)
    headers={'schema_version':'erp-order-headers-v1','source_id':'H1',
        **{key:n[key] for key in ('erp_capture_id','synthetic','sample_only','queried_at','completed_at')},
        'requested_order_ids':['O1'],'orders':{'O1':{'orderid':'O1','shopid':'SHOP-ID','shopname':'SHOPPH','platform':'Tiktok','ref':'H1/O1'}}}
    result=prepare_order_losses(c,n,s,order_headers=headers)
    assert not result['orders']
    assert any(issue['code']=='conflicting_order_finance' for issue in result['issues'])
    assert result['shop_scopes']['O1']['shop_id']=='SHOP-ID'


def test_platform_sku_trailing_ascii_space_is_preserved_in_exact_mapping():
    from app.adapters.erp_order_loss import prepare_order_losses
    c,n,s=inputs();s['variants'][0]['sellerSku']='PLATFORM-S1 '
    result=prepare_order_losses(c,n,s)
    assert not result['issues']
    assert result['orders'][0]['lines'][0]['mapping']['key']['sku']=='PLATFORM-S1 '


@pytest.mark.parametrize('bad',['','   ','S1\t','S1\n','https://example.test','S1?x'])
def test_platform_sku_invalid_text_remains_rejected(bad):
    from app.adapters.erp_pricing import MappedKey
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        MappedKey(platform='TikTok',shop_id='SHOP',site='PH',product_id='P1',item_id='I1',sku=bad,warehouse='WH')


def test_raw_platform_sku_does_not_relax_product_id_or_merge_sku_identity():
    from app.adapters.erp_pricing import MappedKey
    from pydantic import ValidationError
    values=dict(platform='TikTok',shop_id='SHOP',site='PH',product_id='P1',item_id='I1',sku='S1',warehouse='WH')
    first=MappedKey(**values);second=MappedKey(**dict(values,sku='S1 '))
    assert first.identity()!=second.identity() and first.target_identity()==second.target_identity()
    with pytest.raises(ValidationError):MappedKey(**dict(values,product_id='P1 '))
