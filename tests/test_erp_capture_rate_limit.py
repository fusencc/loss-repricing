import json

import httpx
import pytest
from app.adapters import erp_capture
from app.config import Settings


@pytest.mark.parametrize('limited', ['list', 'detail'])
def test_rate_limit_skips_only_the_affected_sample_read(limited):
    lists, orders = [], []

    def handler(request):
        path = request.url.path
        if path.endswith('/login'):
            return httpx.Response(200, json={'code': 200, 'success': True, 'data': {'token': 'secret'}})
        if '/findDetail/' in path:
            order = path.rsplit('/', 1)[1]
            orders.append(order)
            if limited == 'detail' and order == 'O2':
                return httpx.Response(200, json={'code': 500003, 'msg': 'private-upstream-text'})
            return httpx.Response(200, json={'code': 0, 'data': {'detaillist': [{'sku': order}]}})
        status = json.loads(request.content)['status']
        lists.append((path, status))
        if limited == 'list' and (path, status) == ('/Api/deficit/list', 3):
            return httpx.Response(200, json={'code': 500003, 'msg': 'private-upstream-text'})
        ids = ['O1', 'O2', 'O3'] if path == '/Api/deficit/list' else ['O4']
        return httpx.Response(200, json={'code': 0, 'data': {
            'total': len(ids), 'records': [{'orderid': order} for order in ids]}})

    settings = Settings(_env_file=None, erp_username='u', erp_password='p')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = erp_capture.capture_erp_sample(settings, limit=3, client=client)
    assert lists == [(path, status) for path in ('/Api/deficit/list', '/Api/order/list') for status in (4, 3)]
    assert orders == ['O1', 'O2', 'O3', 'O4']
    assert result['rate_limit_observed'] and not result['capture_complete']
    details = result['details']
    assert [r['sku'] for d in details for r in d['rows']] == (
        ['O1', 'O3', 'O4'] if limited == 'detail' else orders)
    failed = details[1] if limited == 'detail' else result['groups'][1]
    assert failed['error_kind'] == 'rate_limited' and failed['business_code'] == 500003
    assert 'private-upstream-text' not in str(result)


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
