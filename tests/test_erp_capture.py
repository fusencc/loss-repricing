import json

import httpx
import pytest
from app.config import Settings
from app.adapters.erp_capture import capture_erp_sample


def test_four_groups_detail_whitelist_and_partial_failure():
    bodies = []
    def handler(request):
        body = json.loads(request.content) if request.content else None
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json={'code': 200, 'success': True, 'data': {'token': 'web-token'}})
        assert request.headers['authorization'] == 'web-token'
        if '/findDetail/' in request.url.path:
            return httpx.Response(200, content='{"code":0,"data":{"detaillist":[{"sku":"SKU-X","qty":2,"currency":"PHP","sellPrice":3.749751,"itempriceinclvat":"1734972991025547169","buyerName":"private"}]}}')
        bodies.append(body)
        if body['status'] == 3 and '/deficit/' in request.url.path:
            return httpx.Response(403, json={'secret': 'private'})
        return httpx.Response(200, json={'code': 0, 'data': {'records': [{'orderid': 'synthetic-order', 'grossprofit': -1, 'buyerName': 'private'}], 'total': 100}})
    settings = Settings(_env_file=None, erp_username='user', erp_password='password')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_erp_sample(settings, limit=2, client=client)
    assert len(result['groups']) == 4
    assert result['complete'] is False and result['pricing_ready'] is False
    assert len([g for g in result['groups'] if g['error']]) == 1
    blocked = [b for b in bodies if 'jzfh' in b]
    assert {b['status'] for b in blocked} == {3, 4}
    assert all(b['jzfh'] == '1' and b['blocs'] == '集团二部' for b in blocked)
    assert all('balance' not in b for b in bodies)
    line = result['details'][0]['rows'][0]
    assert line['sellPrice'] == '3.749751'
    assert line['itempriceinclvat'] == '1734972991025547169'
    assert 'buyerName' not in str(result) and 'private' not in str(result)
    assert result['details'][0]['loss_allocation_confirmed'] is False


def test_business_error_preserves_only_numeric_code():
    def handler(request):
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json={'code': 200, 'success': True, 'data': {'token': 'token'}})
        return httpx.Response(200, json={'code': 500002, 'msg': 'private-account-data'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_erp_sample(Settings(_env_file=None, erp_username='user', erp_password='password'), client=client)
    assert all('500002' in g['error'] for g in result['groups'])
    assert 'private-account-data' not in str(result)


def test_missing_credentials_and_invalid_limit_never_contact_network():
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('unexpected request'))) as client:
        from app.adapters.base import SourceUnavailable
        with pytest.raises(SourceUnavailable):
            capture_erp_sample(Settings(_env_file=None), client=client)
        with pytest.raises(ValueError):
            capture_erp_sample(Settings(_env_file=None), limit=11, client=client)


def test_sample_route_requires_admin_and_validates_limit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    import app.api.erp_rates as route
    import app.api.auth as auth
    from types import SimpleNamespace
    settings = Settings(_env_file=None, runtime_dir=tmp_path,
                        database_url=f"sqlite:///{tmp_path/'sample.db'}",
                        admin_password='local-test', session_secret='a'*40, coordination_required=False)
    calls = []
    def capture(settings, *, limit):
        calls.append(limit)
        return {'pricing_ready': False, 'sample_only': True}
    monkeypatch.setattr(route, 'capture_erp_sample', capture)
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/sources/erp/sample').status_code == 401
        client.post('/api/auth/login', json={'username': 'admin', 'password': 'local-test'})
        assert client.get('/api/sources/erp/sample?limit=11').status_code == 422
        assert calls == []
        assert client.get('/api/sources/erp/sample?limit=2').json()['pricing_ready'] is False
        assert calls == [2]
        monkeypatch.setattr(auth, 'require_user', lambda r: SimpleNamespace(role='sales'))
        assert client.get('/api/sources/erp/sample').status_code == 403
