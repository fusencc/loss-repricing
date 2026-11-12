import httpx
from app.adapters import erp_capture
from app.config import Settings


def test_account_rate_limit_stops_future_network_reads_and_keeps_partial_results():
    paths=[]
    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/login'):
            return httpx.Response(200,json={'code':200,'success':True,'data':{'token':'secret'}})
        if request.url.path.endswith('/findDetail/O1'):
            return httpx.Response(200,json={'code':0,'data':{'detaillist':[{'sku':'SKU','qty':1}]}})
        if request.url.path.endswith('/findDetail/O2'):
            return httpx.Response(200,json={'code':500003,'msg':'do-not-return-upstream-text'})
        return httpx.Response(200,json={'code':0,'data':{'total':2,'records':[
            {'orderid':'O1','platform':'Tiktok'},{'orderid':'O2','platform':'Tiktok'}]}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=erp_capture.capture_erp_sample(Settings(_env_file=None,erp_username='u',erp_password='p'),limit=2,client=client)
    assert len(paths)==4
    assert result['stopped_by_rate_limit'] and not result['capture_complete']
    assert len(result['groups'][0]['rows'])==2
    assert result['summary']['unique_orders']==2
    assert result['details'][0]['rows'][0]['sku']=='SKU'
    assert result['details'][1]['error_kind']=='rate_limited'
    assert len(result['not_read_groups'])==3
    assert 'do-not-return-upstream-text' not in str(result)


def test_owned_client_spaces_requests_by_configured_interval(monkeypatch):
    clock=[0.0];waits=[]
    def sleep(seconds):waits.append(seconds);clock[0]+=seconds
    def handler(request):
        if request.url.path.endswith('/login'):
            return httpx.Response(200,json={'code':200,'success':True,'data':{'token':'secret'}})
        return httpx.Response(200,json={'code':0,'data':{'records':[],'total':0}})
    client=httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(erp_capture.httpx,'Client',lambda **kwargs:client)
    monkeypatch.setattr(erp_capture,'monotonic',lambda:clock[0],raising=False)
    monkeypatch.setattr(erp_capture,'sleep',sleep,raising=False)
    erp_capture.capture_erp_sample(Settings(_env_file=None,erp_username='u',erp_password='p',erp_capture_request_interval_seconds=16),limit=1)
    assert waits==[16,16,16,16]
