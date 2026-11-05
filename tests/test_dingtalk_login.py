"""DingTalk API login boundaries; all identity exchange is mocked, no external calls."""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Batch, Item
from app.models.coordination import Ownership, UserAccount
from app.services.coordination import scope_key
from app.services.store import stamp


@pytest.fixture
def ding_login_context(tmp_path,monkeypatch):
    config=Settings(database_url=f"sqlite:///{tmp_path/'dingtalk-login.db'}",runtime_dir=tmp_path,
                    admin_password='isolated-admin-password',session_secret='isolated-session-secret-at-least32',
                    ding_enabled=True,ding_corp_id='test-corp',ding_client_id='test-client',
                    ding_client_secret='fixture-secret-never-return',ding_agent_id='123',_env_file=None)
    upstream=Mock()
    upstream.user_from_code.return_value={'userid':'ding-seller','name':'Upstream Name'}
    constructor=Mock(return_value=upstream)
    monkeypatch.setattr('app.adapters.dingtalk.DingTalkClient',constructor)
    app=create_app(config)
    with app.state.factory.begin() as session:
        for uid,userid in [('seller','ding-seller'),('other','ding-other')]:
            session.add(UserAccount(id=uid,username=uid,display_name='Local '+uid,role='sales',
                password_hash='not-used-by-sso',active=True,session_version=1,dingtalk_userid=userid,
                created_at=stamp(),updated_at=stamp()))
        session.add(Batch(id='b',run_key='b',business_date='2026-11-05',mode='demo',status='done',
                          created_at=stamp(),snapshot={},summary={}))
        session.flush()
        for uid,shop in [('seller','my-shop'),('other','other-shop')]:
            key={'platform':'demo','shop_id':shop,'site':'MY','product_id':'p',
                 'item_id':uid,'sku':uid,'warehouse':'w'}
            session.add(Item(id=uid,batch_id='b',variant_id=uid,key=key,status='待处理',phase='blocked',
                             reason='',decision={},updated_at=stamp()))
            session.add(Ownership(id=uid,scope_key=scope_key('demo',shop,'MY'),platform='demo',
                                  shop_id=shop,site='MY',owner_id=uid,autonomous=False))
    with TestClient(app) as client:
        yield client,upstream,constructor


def test_bound_active_sales_gets_local_session_with_current_scope(ding_login_context):
    client,upstream,constructor=ding_login_context
    response=client.post('/api/auth/dingtalk',json={'code':'single-use-code'})
    assert response.status_code==200
    assert response.json()['id']=='seller'
    assert response.json()['role']=='sales'
    assert response.json()['display_name']=='Local seller'
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=strict' in response.headers['set-cookie']
    assert client.get('/api/auth/me').json()['id']=='seller'
    assert [item['id'] for item in client.get('/api/items').json()]==['seller']
    assert client.get('/api/items/other').status_code==404
    assert client.post('/api/items/other/control',json={'action':'takeover','reason':'no access','version':0}).status_code==404
    assert client.get('/api/users').status_code==403
    upstream.user_from_code.assert_called_once_with('single-use-code')
    upstream.close.assert_called_once_with()
    constructor.assert_called_once()


@pytest.mark.parametrize('case',['unbound','disabled'])
def test_unknown_or_disabled_identity_cannot_login_or_self_register(ding_login_context,case):
    client,upstream,_=ding_login_context
    if case=='unbound':
        upstream.user_from_code.return_value={'userid':'unknown-person','name':'No local registration'}
    else:
        with client.app.state.factory.begin() as session:
            session.get(UserAccount,'seller').active=False
    response=client.post('/api/auth/dingtalk',json={'code':'one-time-auth-code'})
    assert response.status_code==403
    assert 'lr_session' not in client.cookies
    assert client.get('/api/auth/me').status_code==401
    with client.app.state.factory() as session:
        assert session.query(UserAccount).count()==2
    upstream.close.assert_called_once_with()


def test_disabled_dingtalk_switch_does_not_construct_external_client(ding_login_context):
    client,upstream,constructor=ding_login_context
    client.app.state.config.ding_enabled=False
    response=client.post('/api/auth/dingtalk',json={'code':'unused-auth-code'})
    assert response.status_code==409
    assert client.get('/api/auth/dingtalk-config').json()['enabled'] is False
    constructor.assert_not_called()
    upstream.user_from_code.assert_not_called()
    upstream.close.assert_not_called()


def test_exchange_exception_is_redacted_and_client_always_closed(ding_login_context,caplog):
    client,upstream,_=ding_login_context
    code='private-one-use-code'
    raw='raw-secret-token-private'
    upstream.user_from_code.side_effect=RuntimeError(f'https://example.com/?access_token={raw}&code={code}')
    response=client.post('/api/auth/dingtalk',json={'code':code})
    assert response.status_code==401
    assert response.json()['detail']=='钉钉登录验证失败'
    assert code not in response.text+caplog.text
    assert raw not in response.text+caplog.text
    assert client.app.state.config.ding_client_secret not in response.text+caplog.text
    assert 'lr_session' not in client.cookies
    upstream.close.assert_called_once_with()
