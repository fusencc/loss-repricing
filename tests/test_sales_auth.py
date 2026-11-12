import logging
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from app.config import Settings
from app.adapters.base import SourceUnavailable
from app.adapters.sales_auth import ORIGIN, SalesAuth


def settings(**kwargs):
    return Settings(_env_file=None, vendor_username='own-account', vendor_password='own-password', **kwargs)


def test_form_login_reuse_and_expiry_renewal_are_private(caplog):
    clock=[100.0]; calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.path == '/Api/api-auth/oauth/user/token'
        assert request.headers['content-type'].startswith('application/x-www-form-urlencoded')
        assert request.content == b'username=own-account&password=own-password'
        assert request.headers['client_id'] and request.headers['client_secret']
        return httpx.Response(200,json={'access_token':f'private-{len(calls)}','expires_in':179999,
                                       'token_type':'bearer','scope':'app','refresh_token':'private-refresh'})
    with caplog.at_level(logging.DEBUG), httpx.Client(transport=httpx.MockTransport(handler)) as client:
        auth=SalesAuth(settings(),client,clock=lambda:clock[0])
        assert auth.get_token() == 'private-1'
        clock[0]+=179938
        assert auth.get_token() == 'private-1'
        clock[0]+=2
        assert auth.get_token() == 'private-2'
        assert auth.summary()['expires_in_seconds'] == 179999
        assert auth.summary()['scope'] == 'app'
    assert len(calls)==2
    assert not any(secret in caplog.text for secret in ['own-password','private-','oauth/user/token'])
    assert 'private-' not in repr(auth)


def test_short_lifetime_and_concurrent_calls_only_login_once():
    clock=[0]; calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'access_token':str(len(calls)),'expires_in':10,'token_type':'bearer'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        auth=SalesAuth(settings(),client,clock=lambda:clock[0])
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(lambda _:auth.get_token(),range(8)))==['1']*8
        clock[0]=8
        assert auth.get_token()=='1'
        clock[0]=10
        assert auth.get_token()=='2'
        assert auth.get_token(force_login=True)=='3'


@pytest.mark.parametrize('payload',[
    {'resp_code':400,'resp_msg':'own-password'},
    {'access_token':'private','expires_in':0},
    {'access_token':'private','expires_in':True},
    {'access_token':'private','expires_in':'NaN'},
    {'access_token':'private','expires_in':100,'token_type':'other'},
])
def test_bad_login_cannot_cache_a_token(payload):
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload))) as client:
        auth=SalesAuth(settings(),client)
        with pytest.raises(SourceUnavailable) as error:auth.get_token()
        assert 'own-password' not in str(error.value)
        assert auth.summary()['authenticated'] is False


def test_missing_credentials_cannot_contact_server():
    with httpx.Client(transport=httpx.MockTransport(lambda r:pytest.fail('must not request'))) as client:
        auth=SalesAuth(Settings(_env_file=None),client)
        with pytest.raises(SourceUnavailable):auth.get_token()


def test_expired_token_login_failure_does_not_return_old_token():
    clock=[0]; calls=[]
    def handler(request):
        calls.append(request)
        if len(calls)>1:raise httpx.ConnectError('secret-detail',request=request)
        return httpx.Response(200,json={'access_token':'old','expires_in':10})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        auth=SalesAuth(settings(),client,clock=lambda:clock[0])
        auth.get_token();clock[0]=11
        with pytest.raises(SourceUnavailable):auth.get_token()
        assert auth.summary()['authenticated'] is False


@pytest.mark.parametrize('base_url', [ORIGIN, 'https://sales.example.test'])
def test_vendor_requires_account_credentials_on_the_confirmed_origin(tmp_path, monkeypatch, base_url):
    import json
    from app.adapters.http import HttpAdapter

    path = tmp_path / 'contract.json'
    path.write_text(json.dumps({'verified': True, 'services': {'vendor': {
        'base_url': base_url, 'allow_http_for_internal': True,
    }}}), encoding='utf-8')
    if base_url == ORIGIN:
        # A former static-only deployment must fail closed before a business request.
        monkeypatch.setenv('LR_VENDOR_TOKEN', 'retired-static-token')
        config = Settings(_env_file=None, live_contract_path=path, vendor_username='', vendor_password='')
    else:
        config = settings(live_contract_path=path)

    def unexpected_request(request):
        pytest.fail('未确认销售来源不得发送认证或业务请求')

    with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as client:
        adapter = HttpAdapter(config, client=client)
        with pytest.raises(SourceUnavailable):
            adapter._request('GET', 'vendor', '/read')


def test_adapter_reauthenticates_before_request_but_never_retries_business_post(tmp_path):
    import json
    from app.adapters.http import HttpAdapter
    path=tmp_path/'contract.json'
    path.write_text(json.dumps({'verified':True,'services':{'vendor':{
        'base_url':'http://121.46.237.218:8071','allow_http_for_internal':True}}}),encoding='utf-8')
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={'access_token':'fresh-token','expires_in':100})
        assert request.headers['Authorization']=='Bearer fresh-token'
        raise httpx.ReadTimeout('unknown',request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter=HttpAdapter(settings(live_contract_path=path),client=client)
        with pytest.raises(SourceUnavailable):adapter._request('POST','vendor','/synthetic-business')
    assert len(calls)==2
    assert calls[-1].url.path=='/synthetic-business'
