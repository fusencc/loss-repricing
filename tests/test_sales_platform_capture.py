import json
from pathlib import Path
import httpx
import pytest
from app.config import Settings


def run(tmp_path,case='valid'):
    from app.adapters.sales_platform_capture import capture_platform_controls
    calls=[]
    def handler(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={'access_token':'SECRET','expires_in':3600,'token_type':'bearer'})
        calls.append(str(request.url))
        if '/findProductVariants/' in request.url.path:
            return httpx.Response(200,json={'resp_code':0,'datas':[{'shopName':'SHOPPH','productId':'P1','sellerSkuId':'I1'}]})
        if request.method=='POST':
            body=json.loads(request.content)
            assert body['productIdStr']=='P1' and body['blocNames']==['GROUP']
            active=case!='inactive'
            include=(str(body['isPromotion'])==str(int(active)))
            rows=[{'id':'ROW','productId':'P1','shopName':'SHOPPH','bloc':'GROUP'}] if include else []
            return httpx.Response(200,json={'code':0,'data':rows,'count':len(rows)})
        assert request.method=='GET'
        assert request.url.params['shopName']=='SHOPPH' and request.url.params['productId']=='P1'
        rows=[{'id':'I1','seller_sku':'S1','price':{'sale_price':'61.49','currency':'PHP'},
               'status_info':{'status':'NORMAL'},'inventory':[{'quantity':4,'warehouse_id':'W1'}]}]
        if case=='duplicate':rows.append(rows[0])
        return httpx.Response(200,json={'resp_code':0,'datas':{'id':'OTHER' if case=='outside' else 'P1',
            'product_status':'ACTIVATE','status':'ACTIVATE','is_not_for_sale':False,'skus':rows}})
    settings=Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='U',vendor_password='P',erp_capture_bloc='GROUP')
    capture={'capture_id':'C1','synthetic':True,'sample_only':False}
    targets=[{'erp_shop':'SHOPPH','product_id':'P1','item_id':'I1'},{'erp_shop':'SHOPPH','product_id':'P1','item_id':'I1'}]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_platform_controls(settings,capture,targets,client=client)
    return result,calls


def test_current_platform_state_is_bound_to_requested_product_and_saved_once(tmp_path):
    result,calls=run(tmp_path)
    assert result['acquisition_complete'] and len(calls)==4
    assert result['records'][0]['company_activity_locked'] is True
    assert result['records'][0]['current_variants'][0]['sellerSkuId']=='I1'
    assert result['records'][0]['data']['skus'][0]['price']['sale_price']=='61.49'
    assert json.loads(Path(result['artifact_file']).read_text())==result
    assert all('Bearer SECRET' not in p.read_text() for p in tmp_path.rglob('*.json'))


@pytest.mark.parametrize('case',['outside','duplicate'])
def test_wrong_product_or_duplicate_platform_skus_never_supply_a_state(tmp_path,case):
    result,_=run(tmp_path,case)
    assert not result['acquisition_complete'] and not result['records'] and result['issues']



def test_no_activity_comes_from_explicit_company_filter_not_missing_variant_fields(tmp_path):
    result,_=run(tmp_path,'inactive')
    assert result['acquisition_complete']
    assert result['records'][0]['company_activity_locked'] is False
