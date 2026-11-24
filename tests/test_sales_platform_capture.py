import json
from pathlib import Path
import httpx
import pytest
from app.config import Settings


def run(tmp_path,case='valid', **options):
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
    settings=Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='U',vendor_password='P',erp_capture_bloc='GROUP',sales_request_interval_seconds=0)
    capture={'capture_id':'C1','synthetic':True,'sample_only':False}
    targets=[{'erp_shop':'SHOPPH','product_id':'P1','item_id':'I1'},{'erp_shop':'SHOPPH','product_id':'P1','item_id':'I1'}]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_platform_controls(settings,capture,targets,client=client,**options)
    return result,calls


def test_current_platform_state_is_bound_to_requested_product_and_saved_once(tmp_path):
    result,calls=run(tmp_path)
    assert result['acquisition_complete'] and len(calls)==4
    assert result['records'][0]['company_activity_locked'] is True
    assert result['records'][0]['observed_at'] == result['files'][0]['received_at']
    assert result['records'][0]['observed_at'] <= result['records'][0]['platform_price_observed_at']
    assert result['records'][0]['current_variants'][0]['sellerSkuId']=='I1'
    assert result['records'][0]['data']['skus'][0]['price']['sale_price']=='61.49'
    assert json.loads(Path(result['artifact_file']).read_text())==result
    assert all('Bearer SECRET' not in p.read_text() for p in tmp_path.rglob('*.json'))


@pytest.mark.parametrize(('case', 'active'), [('valid', True), ('inactive', False)])
def test_export_activity_checks_only_the_observed_state(tmp_path, case, active):
    result, calls = run(tmp_path, case, activity_hints={('SHOPPH', 'P1'): active})
    assert result['acquisition_complete']
    assert result['records'][0]['company_activity_locked'] is active
    assert sum('/findByPage' in path for path in calls) == 1


@pytest.mark.parametrize('case',['outside','duplicate'])
def test_wrong_product_or_duplicate_platform_skus_never_supply_a_state(tmp_path,case):
    result,_=run(tmp_path,case)
    assert not result['acquisition_complete'] and not result['records'] and result['issues']



def test_no_activity_comes_from_explicit_company_filter_not_missing_variant_fields(tmp_path):
    result,_=run(tmp_path,'inactive')
    assert result['acquisition_complete']
    assert result['records'][0]['company_activity_locked'] is False


@pytest.mark.parametrize('case', ['valid', 'conflict', 'duplicate', 'truncated', 'second_page', 'limited', 'variant_limited'])
def test_batch_activity_scope_and_pacing(tmp_path, monkeypatch, case):
    from app.adapters import sales_auth
    from app.adapters.sales_platform_capture import capture_platform_controls
    clock = [0]
    monkeypatch.setattr(sales_auth, 'monotonic', lambda: clock[0])
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(sales_auth, 'sleep', sleep)
    requests = []
    def parent(shop, product):
        return dict(shopName=shop, productId=product, bloc='GROUP')
    first, second = parent('SHOPPH', 'P1'), parent('SHOPPH', 'P2')

    def handler(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token':'TOKEN','expires_in':3600})
        requests.append((request.url.path, clock[0]))
        if request.url.path.endswith('/findByPage'):
            body = json.loads(request.content)
            assert body['productIdStr'] == 'P1 P2'
            assert body['blocNames'] == ['GROUP'] and body['limit'] == 100
            if body['isPromotion'] == '0':
                if case == 'limited':
                    return httpx.Response(429)
                rows = [second, first] if case == 'conflict' else [second]
                count = len(rows)
            else:
                assert type(body['isPromotion']) is int and body['isPromotion'] == 1
                rows = [first, first] if case == 'duplicate' else [first]
                count = len(rows)
                if case == 'second_page':
                    count = 101
                    rows = ([parent(f'OTHER{i}PH', 'P1') for i in range(100)]
                            if body['page'] == 1 else [first])
                if case == 'truncated':
                    count += 1
            return httpx.Response(200,json={'code':0,'count':count,'data':rows})
        product = request.url.path.rsplit('/', 1)[-1] if request.method == 'POST' else request.url.params['productId']
        if request.method == 'GET':
            return httpx.Response(200, json={'resp_code':0,'datas':{'id':product,'skus':[{'id':'ITEM'}]}})
        if case == 'variant_limited':
            return httpx.Response(429)
        return httpx.Response(200, json={'resp_code':0,'datas':[
            {'shopName':'SHOPPH','productId':product,'sellerSkuId':'ITEM'}]})

    settings = Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='U',vendor_password='P',
                        erp_capture_bloc='GROUP',sales_request_interval_seconds=2)
    targets = [dict(erp_shop='SHOPPH',product_id=product,item_id='ITEM') for product in ('P1','P2')]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_platform_controls(settings, {'capture_id':'C'}, targets, client=client)
    records = {r['product_id']:r['company_activity_locked'] for r in result['records']}
    assert records == ({'P1':True,'P2':False} if case in ('valid','second_page') else
                       {'P2':False} if case in ('duplicate','conflict') else {})
    assert len(requests) == {'valid':6,'second_page':7,'duplicate':4,'conflict':4,'truncated':2,'limited':1,'variant_limited':4}[case]
    assert [time for _, time in requests] == list(range(0, 2 * len(requests), 2))
    if case in ('limited','variant_limited'):
        assert result['stopped_http_status'] == 429


@pytest.mark.parametrize('interval, count, first_size', [(0,9,8),(30,4,3)])
def test_invalid_activity_page_does_not_stop_next_product_batch(tmp_path, monkeypatch, interval, count, first_size):
    from app.adapters import sales_auth
    from app.adapters.sales_platform_capture import capture_platform_controls
    clock = [0]
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(sales_auth, 'sleep', sleep)
    monkeypatch.setattr(sales_auth, 'monotonic', lambda: clock[0])
    products = [f'P{i:03d}' for i in range(count)]
    activity_requests = []
    def handler(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={'access_token':'TOKEN','expires_in':3600})
        assert request.url.path.endswith('/findByPage')
        body = json.loads(request.content)
        batch = body['productIdStr'].split(' ')
        activity_requests.append((batch, body['isPromotion']))
        # 第一批缺页，第二批两筛选查询完整且均未命中，不得猜无活动。
        return httpx.Response(200,json={'code':0,'count':1 if len(batch)>1 else 0,'data':[]})
    settings = Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='U',vendor_password='P',
                        sales_request_interval_seconds=interval)
    targets = [dict(erp_shop='SHOPPH',product_id=product,item_id='ITEM') for product in products]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_platform_controls(settings, {'capture_id':'C'}, targets, client=client)
    assert activity_requests == [(products[:first_size],'0'),(products[first_size:],'0'),(products[first_size:],1)]
    assert not result['records']
    assert [issue['code'] for issue in result['issues']] == [
        'company_activity_batch_unverified','company_activity_state_conflicting_or_missing']
