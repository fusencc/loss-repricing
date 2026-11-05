import json
import logging

import httpx
import pytest
from app.adapters.base import SourceUnavailable
from app.adapters.erp_rates import query_erp_rates
from app.config import Settings


def config(**kwargs):
    return Settings(_env_file=None, erp_username='test-user', erp_password='secret-password', **kwargs)


def reply(rows):
    return {'success': True, 'code': 200, 'now': 1793861784872, 'hasNext': False, 'data': rows}


def test_login_query_decimal_and_unknown_effective_date(caplog):
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/login'):
            assert json.loads(request.content) == {'username': 'test-user', 'password': 'secret-password'}
            return httpx.Response(200, json={'success': True, 'code': 200, 'data': {'accessToken': 'secret-token'}})
        assert request.headers['Authorization'] == 'Bearer secret-token'
        assert request.method == 'GET'
        return httpx.Response(200, content=json.dumps(reply([
            {'currency': 'CNY', 'rate': 1}, {'currency': 'MYR', 'rate': '1.6225'},
            {'currency': 'PHP', 'rate': '0.1039'}, {'currency': 'VND', 'rate': '0.000253'},
        ])))
    with caplog.at_level(logging.DEBUG), httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = query_erp_rates(config(), client=client)
    assert len(requests) == 2
    assert result['effective_date'] is None and result['pricing_ready'] is False
    assert result['direction_confirmed'] is False
    assert result['rows'][1]['source_rate'] == '1.6225'
    assert result['rows'][1]['units_per_cny'] == '0.61633281972265023112480739599383667180277349768875'
    assert result['rows'][3]['source_rate'] == '0.000253'
    assert result['queried_at'].endswith('+00:00')
    assert 'secret-token' not in caplog.text and 'secret-password' not in caplog.text
    assert '/innerOpen/' not in caplog.text


def test_bad_currency_does_not_hide_other_rates():
    rows = [{'currency': 'MYR', 'rate': '1.6225'}, {'currency': 'THB', 'rate': 0},
            {'currency': 'test', 'rate': 2}, {'currency': 'MYR', 'rate': 2},
            {'currency': 'PHP', 'rate': 'NaN'}, {'currency': 'SGD', 'rate': True},
            {'currency': 'VND', 'rate': '0.000253'}]
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply(rows)))) as client:
        result = query_erp_rates(config(erp_token='token'), client=client)
    assert all(r['units_per_cny'] is None for r in result['rows'][:-1])
    assert result['rows'][-1]['error'] is None
    assert result['rows'][0]['error'] == result['rows'][3]['error']


@pytest.mark.parametrize('payload', [
    {'success': False, 'code': 500, 'msg': 'secret-response'},
    {'success': True, 'code': 200, 'data': {}},
    {**reply([]), 'hasNext': True},
])
def test_unusable_list_is_not_empty_success(payload):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
        with pytest.raises(SourceUnavailable) as error:
            query_erp_rates(config(erp_token='token'), client=client)
    assert 'secret-response' not in str(error.value)


def test_missing_credentials_does_not_send_request():
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('must not request'))) as client:
        with pytest.raises(SourceUnavailable):
            query_erp_rates(Settings(_env_file=None), client=client)


def test_login_token_field_and_invalid_credentials_response():
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json={'success': True, 'code': 200, 'data': {'token': 'Bearer abc'}})
        assert request.headers['Authorization'] == 'Bearer abc'
        return httpx.Response(200, json=reply([]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert query_erp_rates(config(), client=client)['rows'] == []
    assert len(seen) == 2
    for payload in ({'success': False, 'code': 401}, {'success': True, 'code': 200, 'data': {}}):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
            with pytest.raises(SourceUnavailable):
                query_erp_rates(config(), client=client)


def test_network_failure_is_sanitized_and_unknown_base_not_contacted():
    def failed(request):
        raise httpx.ReadTimeout('secret-url-and-token', request=request)
    with httpx.Client(transport=httpx.MockTransport(failed)) as client:
        with pytest.raises(SourceUnavailable) as error:
            query_erp_rates(config(erp_token='token'), client=client)
        assert 'secret' not in str(error.value)
        with pytest.raises(SourceUnavailable):
            query_erp_rates(config(erp_open_api_url='https://unconfirmed.example'), client=client)


def test_admin_api_is_readonly_and_reports_upstream_failure(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    import app.api.erp_rates as route
    import app.api.auth as auth
    from types import SimpleNamespace
    settings = config(database_url=f"sqlite:///{tmp_path/'rates.db'}", runtime_dir=tmp_path,
                      admin_password='local-test', session_secret='a'*40, coordination_required=False)
    calls = []
    def query(settings):
        calls.append(settings)
        return {'rows': [], 'effective_date': None, 'pricing_ready': False}
    monkeypatch.setattr(route, 'query_erp_rates', query)
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/sources/erp/rates').status_code == 401
        assert not calls
        assert client.post('/api/auth/login', json={'username': 'admin', 'password': 'local-test'}).status_code == 200
        result = client.get('/api/sources/erp/rates')
        assert result.status_code == 200 and result.json()['pricing_ready'] is False
        assert result.headers['cache-control'] == 'no-store'
        assert calls == [settings]
        def failed(settings):
            raise SourceUnavailable('ERP汇率查询失败')
        monkeypatch.setattr(route, 'query_erp_rates', failed)
        assert client.get('/api/sources/erp/rates').status_code == 503
        monkeypatch.setattr(auth, 'require_user', lambda r: SimpleNamespace(role='sales'))
        assert client.get('/api/sources/erp/rates').status_code == 403
