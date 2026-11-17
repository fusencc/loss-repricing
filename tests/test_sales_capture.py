"""销售来源只读采集：真实业务代码，外部HTTP边界采用合成响应。"""
from hashlib import sha256
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings


def capture():
    return {'capture_id':'erp-capture-test', 'synthetic':True, 'groups':[
        {'rows':[{'orderid':'O1','fields':{'item ID':'P1 P2'}}]},
        {'rows':[{'orderid':'O1','fields':{'item ID':'P1 P2'}}]},
    ]}


def settings(tmp_path):
    return Settings(_env_file=None, runtime_dir=tmp_path, vendor_username='test', vendor_password='test',
                    erp_capture_bloc='SAMPLE-GROUP', sales_request_interval_seconds=0)


def transport(*, wrong_variant=False, drift=False, missing_variant_id=False):
    def handler(request):
        path=request.url.path
        if path.endswith('/token'):
            return httpx.Response(200,json={'access_token':'TEST-SECRET','expires_in':3600,'token_type':'bearer'})
        assert request.headers['Authorization']=='Bearer TEST-SECRET'
        if path.endswith('/api-b/rate/findAll'):
            return httpx.Response(200,json=[{'id':1,'name':'CNY','rate':1},{'id':2,'name':'MYR','rate':'1.6'}])
        if path.endswith('/online/findByPage'):
            body=json.loads(request.content)
            assert body['blocNames']==['SAMPLE-GROUP']
            assert body['productIdStr']=='P1 P2'
            products=[{'id':'L1','productId':'P1','shopName':'SHOP1MY','bloc':'SAMPLE-GROUP','department':'D1','site':'MY'},
                      {'id':'L2','productId':'P2','shopName':'SHOP1MY','bloc':'SAMPLE-GROUP','department':'D1','site':'MY'}]
            if body['page']==1: rows=products[:body['limit']]
            else: rows=products[(body['page']-1)*body['limit']:body['page']*body['limit']]
            return httpx.Response(200,json={'code':0,'count':3 if drift and body['page']>1 else 2,'data':rows})
        if '/online/findProductVariants/' in path:
            body=json.loads(request.content)
            product=body['productId']
            assert body['shopName']=='SHOP1MY' and path.endswith('/SHOP1MY/'+product)
            row={'id':'V1' if product=='P1' else 'V2','productId':'P9' if wrong_variant and product=='P1' else product,
                 'shopName':'SHOP1MY','sellerSkuId':None if missing_variant_id and product=='P2' else ('0001' if product=='P1' else '0002')}
            if product=='P1':
                row.update(localSellerSku='S1',currency='MYR',priceIncludeVat='EXACT_PRICE')
            content=json.dumps({'resp_code':0,'datas':[row]}).replace('"EXACT_PRICE"','12.1234567890123456789')
            return httpx.Response(200,content=content.encode())
        pytest.fail('Unexpected request, especially any price upload: '+path)
    return httpx.MockTransport(handler)


def test_readonly_capture_deduplicates_scope_and_archives_exact_money(tmp_path):
    from app.adapters.sales_capture import capture_sales_sources
    with httpx.Client(transport=transport()) as client:
        result=capture_sales_sources(settings(tmp_path),capture(),client=client,page_size=1)
    assert result['acquisition_complete']
    assert result['summary']=={'requested_products':2,'products':2,'variants':2,'shops':1}
    assert result['variants'][0]['priceIncludeVat']=='12.1234567890123456789'
    assert result['fee_policy'] == 'user_fixed_configuration'
    assert not {'department_rates','overseas_rates','public_rates'} & result.keys()
    assert result['synthetic'] is True and result['pricing_ready'] is False
    assert result['erp_capture_id']=='erp-capture-test'
    artifact=Path(result['artifact_file'])
    assert json.loads(artifact.read_text())==result
    for original in result['files']:
        data=Path(original['file']).read_bytes()
        assert sha256(data).hexdigest()==original['sha256']
    assert 'TEST-SECRET' not in artifact.read_text()


def test_out_of_scope_variant_is_retained_raw_but_not_promoted_as_valid(tmp_path):
    from app.adapters.sales_capture import capture_sales_sources
    with httpx.Client(transport=transport(wrong_variant=True)) as client:
        result=capture_sales_sources(settings(tmp_path),capture(),client=client)
    assert not result['acquisition_complete']
    assert [row['sellerSkuId'] for row in result['variants']]==['0002']
    assert any(row['code']=='variant_scope_mismatch' for row in result['issues'])
    assert any(b'"P9"' in Path(f['file']).read_bytes() for f in result['files'])


def test_parent_pagination_drift_cannot_be_complete_source(tmp_path):
    from app.adapters.sales_capture import capture_sales_sources
    with httpx.Client(transport=transport(drift=True)) as client:
        result=capture_sales_sources(settings(tmp_path),capture(),client=client,page_size=1)
    assert not result['acquisition_complete']
    assert not result['products'] and not result['variants']
    assert any(row['code']=='product_pagination_incomplete' for row in result['issues'])


def test_missing_variant_id_keeps_other_independent_variant(tmp_path):
    from app.adapters.sales_capture import capture_sales_sources
    with httpx.Client(transport=transport(missing_variant_id=True)) as client:
        result=capture_sales_sources(settings(tmp_path),capture(),client=client)
    assert not result['acquisition_complete']
    assert [row['sellerSkuId'] for row in result['variants']]==['0001']
    assert any(row['code']=='invalid_variant_identity' and row['product_id']=='P2' for row in result['issues'])


def test_one_product_batch_server_failure_does_not_skip_independent_batch(tmp_path):
    from app.adapters.sales_capture import capture_sales_sources
    queried=[]
    def handler(request):
        path=request.url.path
        if path.endswith('/token'):
            return httpx.Response(200,json={'access_token':'TEST','expires_in':3600,'token_type':'bearer'})
        if path.endswith('/online/findByPage'):
            body=json.loads(request.content)
            queried.append(body['productIdStr'])
            if body['productIdStr']=='P1':
                return httpx.Response(500,json={'code':500})
            return httpx.Response(200,json={'code':0,'count':1,'data':[
                {'id':'L2','productId':'P2','shopName':'SHOP2','bloc':'SAMPLE-GROUP'}]})
        return httpx.Response(200,json={'resp_code':0,'datas':[]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_sales_sources(settings(tmp_path),capture(),client=client,product_batch_size=1)
    assert queried==['P1','P1','P2']
    assert result['read_retries'][0]['http_recovered'] is False
    assert [row['productId'] for row in result['products']]==['P2']
    assert not result['acquisition_complete']
    assert any(row['code']=='source_http_failed' for row in result['issues'])


def test_current_variant_state_uses_scoped_online_expansion(tmp_path):
    from app.adapters.sales_capture import capture_sales_sources
    base=transport()
    expanded=[]
    def handler(request):
        path=request.url.path
        if '/online/findProductVariants/' in path:
            body=json.loads(request.content)
            product=body['productId'];shop=body['shopName']
            assert path.endswith('/'+shop+'/'+product)
            expanded.append(product)
            return httpx.Response(200,json={'resp_code':0,'datas':[
                {'productId':product,'shopName':shop,'sellerSkuId':'SKU-'+product,
                 'overseasStockSum':114,'currentOverseasProfitMargin':'-14.93',
                 'overseasStockWarehouseInfo':'[{"overseasWarehouses":["PH-WAREHOUSE"],"quantity":114}]'}]})
        if path.endswith('/findProductVariantsByParams'):
            pytest.fail('Batch price projection omits actual overseas state')
        return base.handle_request(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_sales_sources(settings(tmp_path),capture(),client=client)
    assert expanded==['P1','P2']
    assert result['acquisition_complete']
    assert all(row['overseasStockSum']==114 for row in result['variants'])
    assert all(row['currentOverseasProfitMargin']=='-14.93' for row in result['variants'])






def test_null_parent_site_uses_the_frontend_shop_name_contract():
    from app.adapters.sales_capture import capture_sales_sources
    import tempfile
    base=transport()
    def handler(request):
        response=base.handle_request(request)
        if request.url.path.endswith('/online/findByPage'):
            payload=response.json()
            for row in payload['data']:row['site']=None
            return httpx.Response(200,json=payload)
        return response
    with tempfile.TemporaryDirectory() as folder:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result=capture_sales_sources(settings(Path(folder)),capture(),client=client)
    assert result['acquisition_complete']
    assert result['shop_sites']=={'SHOP1MY':'MY'}
    assert all(row['site'] is None for row in result['products'])


def test_fee_fx_uses_the_company_currency_config_and_preserves_update_time(tmp_path):
    from app.adapters.sales_capture import capture_sales_sources
    base=transport()
    def handler(request):
        if request.url.path.endswith('/api-b/rate/findAll'):
            assert request.method=='GET'
            return httpx.Response(200,content=b'[{"id":1,"name":"CNY","rate":1},{"id":2,"name":"MYR","rate":1.6234567890123456789,"updatetime":"2026-11-11 15:20:53"}]')
        return base.handle_request(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_sales_sources(settings(tmp_path),capture(),client=client)
    assert result['currency_rates'][1]['rate']=='1.6234567890123456789'
    assert result['currency_rates'][1]['updatetime']=='2026-11-11 15:20:53'
    assert any(f['stage']=='currency_rate' for f in result['files'])



def test_transient_parent_http_failure_recovers_without_losing_scope_or_originals(tmp_path,monkeypatch):
    from app.adapters.sales_capture import capture_sales_sources
    monkeypatch.setattr('app.adapters.sales_read.sleep',lambda seconds:None)
    normal=transport();attempts=[]
    def handler(request):
        if request.url.path.endswith('/online/findByPage'):
            attempts.append(request.content)
            if len(attempts)==1:return httpx.Response(500,json={'resp_code':'9999'})
        return normal.handle_request(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_sales_sources(settings(tmp_path),capture(),client=client)
    assert result['acquisition_complete'] and result['missing_product_ids']==[]
    assert attempts[0]==attempts[1] and result['read_retries'][0]['http_recovered']
    originals=[entry for entry in result['files'] if entry['stage']=='product']
    assert [entry['http_status'] for entry in originals]==[500,200]
