"""真实操作日志合同的HTTP边界验证；不把公司日志完整误报为平台历史完整。"""
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import httpx

from app.config import Settings


def test_company_history_archives_pages_without_claiming_platform_coverage(tmp_path):
    from app.adapters.sales_price_history import capture_price_history
    seen=[]
    def handler(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={'access_token':'SECRET','expires_in':3600})
        assert request.url.path=='/Api/api-tiktok-sales/operationLog/findByPage'
        body=json.loads(request.content);seen.append(body)
        assert body['shopNames']=='SHOPPH'
        assert body['completeTimeStart']=='2026-11-08 00:00:00'
        assert body['completeTimeEnd']=='2026-11-11 23:59:59'
        if body['type']!=2:
            return httpx.Response(200,json={'resp_code':0,'datas':{'count':0,'data':[]}})
        payload={'resp_code':0,'datas':{'count':2,'data':[
            {'id':str(body['page']),'shopName':'SHOPPH','type':2,'status':2,
             'completeTime':'2026-11-10 12:00:00','priceOld':'10.10','priceNew':'EXACT_PRICE'}]}}
        return httpx.Response(200,content=json.dumps(payload).replace('"EXACT_PRICE"','11.1234567890123456789').encode())
    sales={'source_id':'S1','erp_capture_id':'C1','synthetic':True,'sample_only':False,
           'products':[{'shopName':'SHOPPH'},{'shopName':'SHOPPH'}]}
    settings=Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='test',vendor_password='test')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_price_history(settings,sales,client=client,page_size=1,
            observed_at=datetime(2026,11,11,6,tzinfo=timezone.utc))
    assert len(seen)==len(result['requested_types'])+1 and result['company_log_complete']
    assert result['platform_manual_changes_complete'] is False
    assert result['pricing_ready'] is False and len(result['records'])==2
    assert result['erp_capture_id']=='C1' and result['sales_source_id']=='S1'
    assert result['records'][0]['priceNew']=='11.1234567890123456789'
    for entry in result['files']:
        raw=Path(entry['file']).read_bytes()
        assert sha256(raw).hexdigest()==entry['sha256']
        assert b'SECRET' not in raw


def test_bad_shop_or_date_does_not_become_valid_history(tmp_path):
    from app.adapters.sales_price_history import capture_price_history
    def handler(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={'access_token':'SECRET','expires_in':3600})
        return httpx.Response(200,json={'resp_code':0,'datas':{'count':1,'data':[
            {'id':'1','shopName':'OTHERPH','completeTime':'2026-11-01 00:00:00'}]}})
    sales={'source_id':'S1','erp_capture_id':'C1','synthetic':True,'sample_only':False,
           'products':[{'shopName':'SHOPPH'}]}
    settings=Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='test',vendor_password='test')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_price_history(settings,sales,client=client,
            observed_at=datetime(2026,11,11,6,tzinfo=timezone.utc))
    assert not result['company_log_complete'] and result['records']==[]
    assert result['issues'][0]['code']=='history_scope_mismatch'
    assert result['files'] and Path(result['artifact_file']).is_file()


def test_changing_page_total_cannot_prove_complete_history(tmp_path):
    from app.adapters.sales_price_history import capture_price_history
    def handler(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={'access_token':'SECRET','expires_in':3600})
        body=json.loads(request.content)
        if body['type']!=2:
            return httpx.Response(200,json={'resp_code':0,'datas':{'count':0,'data':[]}})
        return httpx.Response(200,json={'resp_code':0,'datas':{'count':2 if body['page']==1 else 3,'data':[
            {'id':str(body['page']),'type':2,'shopName':'SHOPPH','completeTime':'2026-11-10 12:00:00'}]}})
    sales={'source_id':'S1','erp_capture_id':'C1','synthetic':True,'sample_only':False,
           'products':[{'shopName':'SHOPPH'}]}
    settings=Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='test',vendor_password='test')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_price_history(settings,sales,client=client,page_size=1,
            observed_at=datetime(2026,11,11,6,tzinfo=timezone.utc))
    assert not result['company_log_complete'] and result['records']==[]
    assert result['issues'][0]['code']=='history_pagination_incomplete'
    assert len(result['files'])==len(result['requested_types'])+1


def test_failed_login_stops_source_instead_of_relogging_for_each_shop_batch(tmp_path):
    from app.adapters.sales_price_history import capture_price_history
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        assert request.url.path.endswith('/token')
        return httpx.Response(400,json={'error':'invalid_grant'})
    sales={'source_id':'S1','erp_capture_id':'C1','synthetic':True,'sample_only':False,
           'products':[{'shopName':'AAPH'},{'shopName':'BBPH'}]}
    settings=Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='test',vendor_password='test')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_price_history(settings,sales,client=client,shop_batch_size=1)
    assert len(calls)==1
    assert not result['company_log_complete'] and not result['records']
    assert result['issues'][0]['code']=='history_authentication_failed'
    assert Path(result['artifact_file']).is_file()


def test_unqueryable_shop_is_local_issue_and_other_shop_still_archives(tmp_path):
    from app.adapters.sales_price_history import capture_price_history
    queried=[]
    def handler(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={'access_token':'SECRET','expires_in':3600})
        body=json.loads(request.content);queried.append(body['shopNames'])
        return httpx.Response(200,json={'resp_code':0,'datas':{'count':0,'data':[]}})
    sales={'source_id':'S1','erp_capture_id':'C1','synthetic':True,'sample_only':False,
           'products':[{'shopName':'AAPH'},{'shopName':'SHOP WITH SPACE PH'}]}
    settings=Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='test',vendor_password='test')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_price_history(settings,sales,client=client)
    assert set(queried)=={'AAPH'} and len(queried)==len(result['requested_types']) and not result['company_log_complete']
    assert result['issues'][0]=={'code':'history_shop_not_queryable','shops':['SHOP WITH SPACE PH']}
    assert Path(result['artifact_file']).is_file()


def test_calendar_query_is_trimmed_to_exact_72_hours_after_complete_read(tmp_path):
    from app.adapters.sales_price_history import capture_price_history
    def handler(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={'access_token':'SECRET','expires_in':3600})
        body=json.loads(request.content)
        assert body['completeTimeStart']=='2026-11-08 00:00:00'
        if body['type']!=2:
            return httpx.Response(200,json={'resp_code':0,'datas':{'count':0,'data':[]}})
        return httpx.Response(200,json={'resp_code':0,'datas':{'count':3,'data':[
            {'id':'before','type':2,'shopName':'SHOPPH','completeTime':'2026-11-08 13:59:59'},
            {'id':'boundary','type':2,'shopName':'SHOPPH','completeTime':'2026-11-08 14:00:00'},
            {'id':'after','type':2,'shopName':'SHOPPH','completeTime':'2026-11-11 14:00:01'}]}})
    sales={'source_id':'S1','erp_capture_id':'C1','synthetic':True,'sample_only':False,
           'products':[{'shopName':'SHOPPH'}]}
    settings=Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='test',vendor_password='test')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_price_history(settings,sales,client=client,
            observed_at=datetime(2026,11,11,6,tzinfo=timezone.utc))
    assert result['company_log_complete'] and [r['id'] for r in result['records']]==['boundary']
    assert result['source_record_count']==3 and len(result['excluded_time_records'])==2


def test_history_waits_for_a_real_second_after_subsecond_price_observation(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    import app.adapters.sales_price_history as module
    moments=[datetime(2026,11,11,2,0,0,700000,tzinfo=timezone.utc),datetime(2026,11,11,2,0,1,100,tzinfo=timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return moments.pop(0) if len(moments)>1 else moments[0]
    waits=[]
    monkeypatch.setattr(module,'datetime',Clock)
    monkeypatch.setattr(module,'sleep',lambda seconds:waits.append(seconds),raising=False)
    from app.config import Settings
    sales={'source_id':'S1','erp_capture_id':'C1','synthetic':True,'sample_only':False,'products':[]}
    result=module.capture_price_history(Settings(_env_file=None,runtime_dir=tmp_path),sales,
        not_before=datetime(2026,11,11,2,0,0,500000,tzinfo=timezone.utc))
    assert waits and 0<waits[0]<=1
    assert datetime.fromisoformat(result['window_end'])>=datetime(2026,11,11,2,0,0,500000,tzinfo=timezone.utc)
    assert datetime.fromisoformat(result['window_end'])<=datetime.fromisoformat(result['queried_at'])


def test_explicit_pricing_shop_scope_is_complete_without_querying_other_captured_shops(tmp_path):
    from app.adapters.sales_price_history import capture_price_history
    calls=[]
    def handler(request):
        if request.url.path.endswith('/token'):return httpx.Response(200,json={'access_token':'SECRET','expires_in':3600})
        body=json.loads(request.content);calls.append(body);assert body['shopNames']=='KEEP-PH'
        return httpx.Response(200,json={'resp_code':0,'datas':{'count':0,'data':[]}})
    sales={'source_id':'S1','erp_capture_id':'C1','synthetic':True,'sample_only':False,
        'products':[{'shopName':'KEEP-PH'},{'shopName':'UNRELATED-PH'}]}
    settings=Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='U',vendor_password='P')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_price_history(settings,sales,client=client,shop_scope=['KEEP-PH'])
    assert result['company_log_complete'] and result['requested_shops']==['KEEP-PH']
    assert len(calls)==len(result['requested_types'])
