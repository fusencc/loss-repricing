from types import SimpleNamespace
from unittest.mock import Mock
import httpx
import pytest
from sqlalchemy import select
from app.db import Base, create_database


def test_notification_module_is_available():
    from app.models.notifications import Notice
    assert Notice.__tablename__ == 'notices'


def test_unknown_send_and_accepted_poll_never_resend(tmp_path, monkeypatch):
    from app.models.notifications import Notice
    from app.services.notifications import dispatch_notices
    from app.adapters.dingtalk import DingTalkClient
    from app.services.store import stamp, utcnow
    engine, factory = create_database(f"sqlite:///{tmp_path / 'db'}")
    Base.metadata.create_all(engine)
    with factory.begin() as s:
        s.add(Notice(id='n', dedupe_key='n', recipient_id='admin', batch_id=None,
                     item_ids=[], summary={'title':'x'}, delivery_state='sending',
                     created_at=stamp(utcnow()),updated_at=stamp(utcnow())))
    send=Mock(side_effect=AssertionError('must not resend'))
    monkeypatch.setattr(DingTalkClient,'send_notice',send)
    cfg=SimpleNamespace(notification_mode='live',ding_enabled=True)
    dispatch_notices(factory,cfg)
    dispatch_notices(factory,cfg)
    with factory() as s:
        assert s.get(Notice,'n').delivery_state == 'unknown'
    send.assert_not_called()
    engine.dispose()


def test_dingtalk_fixed_endpoint_and_recipient_result():
    from app.adapters.dingtalk import DingTalkClient
    cfg=SimpleNamespace(ding_client_id='client',ding_client_secret='secret',ding_agent_id='123')
    requests=[]
    def handler(req):
        requests.append(req)
        if req.url.path.endswith('/accessToken'):
            return httpx.Response(200,json={'accessToken':'token','expireIn':7200})
        if req.url.path.endswith('/getuserinfo'):
            return httpx.Response(200,json={'errcode':0,'result':{'userid':'u','name':'N'}})
        if req.url.path.endswith('/asyncsend_v2'):
            return httpx.Response(200,json={'errcode':0,'task_id':1234,'request_id':'r'})
        return httpx.Response(200,json={'errcode':0,'send_result':{'failed_user_id_list':['u'], 'read_user_id_list':['other']}})
    c=DingTalkClient(cfg,transport=httpx.MockTransport(handler))
    assert c.user_from_code('one-use') == {'userid':'u','name':'N'}
    result=c.send_notice('u','Title','Summary','https://example.com/#notifications')
    assert result['state'] == 'accepted'
    assert c.query_notice('1234','u')['state'] == 'failed'
    assert sum(r.url.path.endswith('/accessToken') for r in requests)==1
    assert all(r.url.scheme=='https' for r in requests)


def test_dingtalk_timeout_is_unknown():
    from app.adapters.dingtalk import DingTalkClient
    cfg=SimpleNamespace(ding_client_id='c',ding_client_secret='s',ding_agent_id='1')
    def handler(req):
        if req.url.path.endswith('/accessToken'):
            return httpx.Response(200,json={'accessToken':'token','expireIn':7200})
        raise httpx.ReadTimeout('access_token=private',request=req)
    c=DingTalkClient(cfg,transport=httpx.MockTransport(handler))
    assert c.send_notice('u','t','m','https://example.com/')['state']=='unknown'

@pytest.fixture
def notices_context(tmp_path):
    from app.models import Batch, Item
    from app.models.notifications import Notice
    from app.models.coordination import UserAccount, Ownership
    from app.services.coordination import scope_key
    from app.services.store import stamp
    engine,factory=create_database(f"sqlite:///{tmp_path/'notices.db'}")
    Base.metadata.create_all(engine)
    key={'platform':'tiktok','shop_id':'shop','site':'MY','product_id':'p','item_id':'online','sku':'sku','warehouse':'w'}
    with factory.begin() as s:
        for uid,role in [('seller','sales'),('other','sales'),('manager','manager')]:
            s.add(UserAccount(id=uid,username=uid,display_name=uid,role=role,password_hash='unused',
                              dingtalk_userid='ding-'+uid,created_at=stamp(),updated_at=stamp()))
        s.add(Ownership(id='o',scope_key=scope_key('tiktok','shop','MY'),platform='tiktok',shop_id='shop',site='MY',owner_id='seller',supervisor_id='manager'))
        s.add(Batch(id='b',run_key='b',business_date='2026-11-05',mode='demo',status='done',created_at=stamp(),snapshot={},summary={}))
        s.flush()
        s.add(Item(id='i',batch_id='b',variant_id='v',key=key,status='待处理',phase='failed',reason='reason',decision={},updated_at=stamp()))
    cfg=SimpleNamespace(notification_mode='demo',ding_enabled=False,session_secret='x'*32,admin_username='admin',
                        ding_corp_id='corp',ding_client_id='client',ding_client_secret='secret',ding_agent_id='123',public_base_url='https://example.com')
    yield factory,cfg
    engine.dispose()


def test_batch_deduplicated_and_demo_is_visibly_synthetic(notices_context):
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices,dispatch_notices
    factory,cfg=notices_context
    assert enqueue_batch_notices(factory,'b')==1
    assert enqueue_batch_notices(factory,'b')==0
    dispatch_notices(factory,cfg)
    with factory() as s:
        n=s.scalar(select(Notice))
        assert n.recipient_id=='seller' and n.item_ids==['i']
        assert n.delivery_state=='demo' and n.synthetic and n.task_id is None
        assert n.summary['exceptions']==1


def test_real_send_persisted_first_then_poll_only(notices_context,monkeypatch):
    from app.models.notifications import Notice
    from app.adapters.dingtalk import DingTalkClient
    from app.services.notifications import enqueue_batch_notices,dispatch_notices
    factory,cfg=notices_context
    cfg.notification_mode='live';cfg.ding_enabled=True
    enqueue_batch_notices(factory,'b')
    def send(self,*args):
        with factory() as s:
            assert s.scalar(select(Notice)).delivery_state=='sending'
        return {'state':'accepted','task_id':'task','request_id':'send-r'}
    monkeypatch.setattr(DingTalkClient,'send_notice',send)
    dispatch_notices(factory,cfg)
    monkeypatch.setattr(DingTalkClient,'send_notice',lambda *a:pytest.fail('accepted must only query'))
    monkeypatch.setattr(DingTalkClient,'query_notice',lambda *a:{'state':'read','request_id':'query-r'})
    dispatch_notices(factory,cfg)
    with factory() as s:
        n=s.scalar(select(Notice))
        assert n.delivery_state=='read' and n.ack_at is None
        assert n.task_id=='task'
        assert {r['request_id'] for r in n.delivery_evidence if r.get('request_id')}=={'send-r','query-r'}


def test_transferred_scope_prevents_send_and_ack(notices_context):
    from app.models.coordination import Ownership
    from app.models.notifications import Notice
    from app.services.coordination import Principal
    from app.services.notifications import enqueue_batch_notices,dispatch_notices,notice_visible
    factory,cfg=notices_context
    enqueue_batch_notices(factory,'b')
    with factory.begin() as s:
        s.get(Ownership,'o').owner_id='other'
    dispatch_notices(factory,cfg)
    with factory() as s:
        n=s.scalar(select(Notice))
        assert n.delivery_state=='cancelled'
        assert not notice_visible(s,Principal('seller','seller','seller','sales'),n)


def test_escalation_once_after24h_only_to_explicit_supervisor(notices_context):
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices,dispatch_notices
    from app.services.store import utcnow,stamp
    from datetime import timedelta
    factory,cfg=notices_context
    enqueue_batch_notices(factory,'b')
    with factory.begin() as s:
        s.scalar(select(Notice)).created_at=stamp(utcnow()-timedelta(hours=25))
    dispatch_notices(factory,cfg);dispatch_notices(factory,cfg)
    with factory() as s:
        children=s.scalars(select(Notice).where(Notice.parent_id.is_not(None))).all()
        assert len(children)==1 and children[0].recipient_id=='manager'
        assert children[0].synthetic


def test_notification_routes_authorization_retry_and_ack(notices_context):
    from app.api.notifications import router
    from app.api.auth import make_session,COOKIE
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    factory,cfg=notices_context
    enqueue_batch_notices(factory,'b')
    app=FastAPI();app.state.factory=factory;app.state.config=cfg;app.include_router(router)
    client=TestClient(app)
    def login(uid):
        client.cookies.set(COOKIE,make_session(uid,cfg.session_secret,user_id=uid,version=1))
    login('seller')
    data=client.get('/api/notifications').json(); assert len(data)==1
    nid=data[0]['id']
    login('other')
    assert client.get('/api/notifications').json()==[]
    assert client.post(f'/api/notifications/{nid}/ack').status_code==404
    login('seller')
    assert client.post(f'/api/notifications/{nid}/ack').status_code==200
    assert client.post('/api/notifications/dispatch').status_code==403
    login('admin')
    assert client.post(f'/api/notifications/{nid}/retry').status_code==409
    with factory.begin() as s:
        n=s.get(Notice,nid);n.delivery_state='failed'
    assert client.post(f'/api/notifications/{nid}/retry').json()['delivery_state']=='pending'
    assert client.get('/api/notification-status').json()['synthetic'] is True

@pytest.mark.parametrize('state',['accepted','unknown','sending','delivered','read','demo'])
def test_retry_rejects_every_state_except_explicit_failed(notices_context,state):
    from app.api.notifications import router
    from app.api.auth import make_session,COOKIE
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    factory,cfg=notices_context
    enqueue_batch_notices(factory,'b')
    with factory.begin() as s:
        n=s.scalar(select(Notice));n.delivery_state=state;nid=n.id
    app=FastAPI();app.state.factory=factory;app.state.config=cfg;app.include_router(router)
    client=TestClient(app);client.cookies.set(COOKIE,make_session('admin',cfg.session_secret))
    assert client.post(f'/api/notifications/{nid}/retry').status_code==409
    with factory() as s:
        assert s.get(Notice,nid).delivery_state==state


def test_missing_owner_routes_web_only_to_admin_without_guessing(notices_context,monkeypatch):
    from app.models.coordination import Ownership
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices,dispatch_notices
    from app.adapters.dingtalk import DingTalkClient
    factory,cfg=notices_context
    with factory.begin() as s:
        s.delete(s.get(Ownership,'o'))
    enqueue_batch_notices(factory,'b')
    cfg.notification_mode='live';cfg.ding_enabled=True
    monkeypatch.setattr(DingTalkClient,'send_notice',lambda *a:pytest.fail('must not guess admin userid'))
    dispatch_notices(factory,cfg)
    with factory() as s:
        n=s.scalar(select(Notice))
        assert n.recipient_id=='admin' and n.summary['unassigned']
        assert n.delivery_state=='pending'


def test_no_supervisor_and_acknowledged_exceptions_do_not_escalate(notices_context):
    from app.models.coordination import Ownership
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices,dispatch_notices
    from app.services.store import stamp,utcnow
    from datetime import timedelta
    factory,cfg=notices_context
    enqueue_batch_notices(factory,'b')
    with factory.begin() as s:
        s.get(Ownership,'o').supervisor_id=None
        s.scalar(select(Notice)).created_at=stamp(utcnow()-timedelta(hours=25))
    dispatch_notices(factory,cfg)
    with factory.begin() as s:
        assert s.query(Notice).count()==1
        s.get(Ownership,'o').supervisor_id='manager'
        s.scalar(select(Notice)).ack_at=stamp()
    dispatch_notices(factory,cfg)
    with factory() as s:
        assert s.query(Notice).count()==1


def test_query_error_never_causes_resend_and_masks_secrets():
    from app.adapters.dingtalk import DingTalkClient
    cfg=SimpleNamespace(ding_client_id='c',ding_client_secret='s',ding_agent_id='1')
    def handler(req):
        if req.url.path.endswith('/accessToken'):
            return httpx.Response(200,json={'accessToken':'token','expireIn':7200})
        raise httpx.ReadTimeout('access_token=private',request=req)
    c=DingTalkClient(cfg,transport=httpx.MockTransport(handler))
    result=c.query_notice('123','u')
    assert result['state']=='accepted'
    assert 'private' not in str(result)


def test_concurrent_dispatch_does_not_recover_active_sender(notices_context,monkeypatch):
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices,dispatch_notices
    from app.adapters.dingtalk import DingTalkClient
    factory,cfg=notices_context
    cfg.notification_mode='live';cfg.ding_enabled=True
    enqueue_batch_notices(factory,'b')
    def send(self,*args):
        nested=dispatch_notices(factory,cfg)
        assert nested.get('busy') is True
        with factory() as s:
            assert s.scalar(select(Notice)).delivery_state=='sending'
        return {'state':'accepted','task_id':'task','request_id':'r'}
    monkeypatch.setattr(DingTalkClient,'send_notice',send)
    dispatch_notices(factory,cfg)
    with factory() as s:
        assert s.scalar(select(Notice)).delivery_state=='accepted'


def test_partial_transfer_rebuilds_recipient_summary_before_send(notices_context,monkeypatch):
    from app.models import Item
    from app.models.coordination import Ownership
    from app.models.notifications import Notice
    from app.services.coordination import scope_key
    from app.services.notifications import enqueue_batch_notices,dispatch_notices
    from app.adapters.dingtalk import DingTalkClient
    from app.services.store import stamp
    from app.domain.types import VariantKey
    factory,cfg=notices_context
    with factory.begin() as s:
        key=dict(s.get(Item,'i').key);key['item_id']='online2'
        s.add(Item(id='i2',batch_id='b',variant_id='v2',key=key,status='已核验',phase='verified',reason='',decision={},updated_at=stamp()))
    enqueue_batch_notices(factory,'b')
    with factory.begin() as s:
        key=s.get(Item,'i2').key
        target=VariantKey.model_validate(key).target_identity()
        s.add(Ownership(id='specific',scope_key=scope_key('tiktok','shop','MY',target),platform='tiktok',shop_id='shop',site='MY',target_id=target,owner_id='other'))
    cfg.notification_mode='live';cfg.ding_enabled=True
    def send(self,userid,title,markdown,url):
        assert '共1条' in markdown
        with factory() as s:
            assert s.scalar(select(Notice)).item_ids==['i']
        return {'state':'accepted','task_id':'t'}
    monkeypatch.setattr(DingTalkClient,'send_notice',send)
    dispatch_notices(factory,cfg)
    with factory() as s:
        assert s.scalar(select(Notice)).summary['total']==1


def test_currently_resolved_exception_does_not_escalate(notices_context):
    from app.models import Item
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices,dispatch_notices
    from app.services.store import utcnow,stamp
    from datetime import timedelta
    factory,cfg=notices_context
    enqueue_batch_notices(factory,'b')
    with factory.begin() as s:
        s.scalar(select(Notice)).created_at=stamp(utcnow()-timedelta(hours=25))
        s.get(Item,'i').status='已核验'
    dispatch_notices(factory,cfg)
    with factory() as s:
        assert s.query(Notice).count()==1


def test_mixed_batch_escalates_only_current_exception_not_stale_summary(notices_context):
    from app.models import Item
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices,dispatch_notices
    from app.services.store import utcnow,stamp
    from datetime import timedelta
    factory,cfg=notices_context
    with factory.begin() as s:
        first=s.get(Item,'i');first.status='已核验'
        key=dict(first.key);key['item_id']='normal'
        s.add(Item(id='normal',batch_id='b',variant_id='normal',key=key,status='已核验',phase='verified',reason='',decision={},updated_at=stamp()))
    enqueue_batch_notices(factory,'b')
    with factory.begin() as s:
        original=s.scalar(select(Notice));original.created_at=stamp(utcnow()-timedelta(hours=25))
        assert original.summary['exceptions']==0
        s.get(Item,'i').status='待核验'
    dispatch_notices(factory,cfg)
    with factory() as s:
        child=s.scalar(select(Notice).where(Notice.parent_id.is_not(None)))
        assert child is not None
        assert child.item_ids==['i']
        assert child.summary['total']==1 and child.summary['exceptions']==1


@pytest.mark.parametrize('state',['accepted','unknown','demo'])
def test_enqueue_refreshes_summary_without_resend_or_ack_change(notices_context,state):
    from app.models import Item
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices
    from app.services.store import stamp
    factory,cfg=notices_context
    enqueue_batch_notices(factory,'b')
    with factory.begin() as s:
        n=s.scalar(select(Notice));n.delivery_state=state;n.task_id='historic-task';n.request_id='historic-request'
        n.ack_at=stamp();n.ack_by='seller';ack=n.ack_at
        n.delivery_evidence=[{'operation':'send','summary':dict(n.summary),'task_id':'historic-task'}]
        s.get(Item,'i').status='已核验'
    assert enqueue_batch_notices(factory,'b')==0
    with factory() as s:
        n=s.scalar(select(Notice))
        assert n.summary['exceptions']==0 and n.summary['statuses']=={'已核验':1}
        assert n.delivery_state==state and n.task_id=='historic-task' and n.request_id=='historic-request'
        assert n.ack_at==ack and n.ack_by=='seller'
        assert n.delivery_evidence[0]['summary']['exceptions']==1


def test_failed_batch_refreshes_existing_notice_without_queuing_new_delivery(notices_context):
    from app.models import Batch, Item, Notice
    from app.services.execution import PricingService
    from app.services.notifications import enqueue_batch_notices

    factory, cfg = notices_context
    cfg.coordination_required = True
    service = PricingService(factory, None, cfg)
    service._summarize("b", refresh_error="刷新失败")
    with factory() as session:
        assert session.scalar(select(Notice)) is None
    enqueue_batch_notices(factory, "b")
    with factory.begin() as session:
        notice = session.scalar(select(Notice))
        notice.delivery_state = "accepted"
        notice.task_id = "original-task"
        session.get(Item, "i").status = "已核验"

    service._summarize("b")
    with factory() as session:
        notice = session.scalars(select(Notice)).one()
        assert notice.summary["statuses"] == {"已核验": 1}
        assert notice.delivery_state == "accepted" and notice.task_id == "original-task"
        assert session.get(Batch, "b").status == "failed"


def test_notice_detail_pages_current_results_and_rechecks_recipient(notices_context):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.auth import COOKIE, make_session
    from app.api.notifications import router
    from app.models import Execution, Item
    from app.models.coordination import Ownership
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices
    from app.services.store import stamp

    factory, cfg = notices_context
    with factory.begin() as session:
        for sku, status in (("checked", "待处理"), ("awaiting", "待核验"), ("protected", "本轮不调")):
            key = dict(session.get(Item, "i").key, item_id=sku, sku=sku)
            session.add(Item(
                id=sku, batch_id="b", variant_id=sku, key=key, status=status, phase="proposed",
                reason="sample reason", decision={"target_actual": "18.72"}, updated_at=stamp(),
            ))
    enqueue_batch_notices(factory, "b")
    with factory.begin() as session:
        notice = session.scalar(select(Notice))
        notice_id = notice.id
        assert notice.summary["statuses"]["待处理"] == 2
        checked = session.get(Item, "checked")
        checked.status, checked.phase = "已核验", "verified"
        session.add(Execution(
            id="checked-execution", item_id="checked", state="verified", variant_id="checked",
            request={}, response={}, evidence={"actual_price": "18.72", "mode": "demo"},
            created_at=stamp(), updated_at=stamp(),
        ))

    app = FastAPI()
    app.state.factory, app.state.config = factory, cfg
    app.include_router(router)
    path = f"/api/notifications/{notice_id}"
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        client.cookies.set(COOKIE, make_session("other", cfg.session_secret, user_id="other", version=1))
        assert client.get(path).status_code == 404

        client.cookies.set(COOKIE, make_session("seller", cfg.session_secret, user_id="seller", version=1))
        page = client.get(path, params={"limit": 2})
        assert page.status_code == 200, page.text
        result = page.json()
        assert result["batch_mode"] == "demo"
        assert result["total"] == 4
        assert result["statuses"] == {"待处理": 1, "待核验": 1, "已核验": 1, "本轮不调": 1}
        assert result["delivery_state"] == "pending" and result["ack_at"] is None
        assert [item["id"] for item in result["items"]] == ["i", "awaiting"]

        last_page = client.get(path, params={"limit": 2, "offset": 2}).json()
        assert [item["id"] for item in last_page["items"]] == ["checked", "protected"]
        assert last_page["items"][0]["evidence"]["actual_price"] == "18.72"
        assert last_page["items"][0]["decision"]["target_actual"] == "18.72"
        assert client.get(path, params={"limit": 101}).status_code == 422

        with factory.begin() as session:
            assert session.get(Notice, notice_id).ack_at is None
            session.get(Ownership, "o").owner_id = "other"
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("base, expected_path", [
    ("https://example.com", "https://example.com"),
    ("https://example.com/workbench/#overview", "https://example.com/workbench"),
])
def test_dingtalk_card_opens_specific_notice_with_scoped_statuses(notices_context, monkeypatch, base, expected_path):
    from app.adapters.dingtalk import DingTalkClient
    from app.models import Item
    from app.models.notifications import Notice
    from app.services.notifications import dispatch_notices, enqueue_batch_notices

    factory, cfg = notices_context
    with factory.begin() as session:
        item = session.get(Item, "i")
        item.key = dict(item.key, sku="SYNTHETIC-SKU-NOT-IN-CARD")
        item.reason = "SYNTHETIC-PRIVATE-REASON"
    enqueue_batch_notices(factory, "b")
    cfg.notification_mode, cfg.ding_enabled = "live", True
    cfg.public_base_url = base
    outbound = []

    def send(self, userid, title, markdown, url):
        with factory() as session:
            saved = session.scalar(select(Notice))
            assert saved.delivery_state == "sending"
            assert url == f"{expected_path}/#notifications/{saved.id}"
        outbound.append((userid, markdown))
        return {"state": "accepted", "task_id": "task"}

    monkeypatch.setattr(DingTalkClient, "send_notice", send)
    dispatch_notices(factory, cfg)
    assert len(outbound) == 1
    userid, markdown = outbound[0]
    assert userid == "ding-seller"
    assert "演示批次" in markdown
    assert "待处理 1 · 待核验 0 · 已核验 0 · 本轮不调 0" in markdown
    assert "SYNTHETIC-" not in markdown


@pytest.mark.parametrize("changed_account, change", [
    ("manager", {"role": "sales"}),
    ("seller", {"active": False}),
])
def test_escalation_dispatch_rechecks_supervisor_permission(notices_context, monkeypatch, changed_account, change):
    from app.adapters.dingtalk import DingTalkClient
    from app.models.coordination import UserAccount
    from app.models.notifications import Notice
    from app.services.notifications import dispatch_notices, enqueue_batch_notices
    from app.services.store import stamp

    factory, cfg = notices_context
    enqueue_batch_notices(factory, "b")
    with factory.begin() as session:
        original = session.scalar(select(Notice))
        original.ack_at = stamp()
        original.delivery_state = "demo"
        for field, value in change.items():
            setattr(session.get(UserAccount, changed_account), field, value)
        session.add(Notice(
            id="escalated", dedupe_key="escalated", batch_id="b", parent_id=original.id,
            item_ids=["i"], recipient_id="manager", summary=original.summary,
            delivery_state="pending", created_at=stamp(), updated_at=stamp(),
        ))
    cfg.notification_mode, cfg.ding_enabled = "live", True
    monkeypatch.setattr(DingTalkClient, "send_notice", lambda *args: pytest.fail("revoked scope must not receive a card"))
    dispatch_notices(factory, cfg)
    with factory() as session:
        assert session.get(Notice, "escalated").delivery_state == "cancelled"


def test_notice_detail_authenticates_in_the_same_read_session(notices_context):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.auth import COOKIE, make_session
    from app.api.notifications import router
    from app.models.coordination import UserAccount
    from app.models.notifications import Notice
    from app.services.notifications import enqueue_batch_notices

    factory, cfg = notices_context
    enqueue_batch_notices(factory, "b")
    with factory.begin() as session:
        notice = session.scalar(select(Notice))
        notice.recipient_id = "manager"
        notice_id = notice.id

    # Simulate revocation in the gap between an identity-only session and an independent data session.
    # One read session either authorizes the original view or observes the revocation at its start.
    opened = 0
    def factory_with_revocation():
        nonlocal opened
        opened += 1
        if opened == 2:
            with factory.begin() as session:
                account = session.get(UserAccount, "manager")
                account.role = "sales"
                account.session_version += 1
        return factory()

    app = FastAPI()
    app.state.factory, app.state.config = factory_with_revocation, cfg
    app.include_router(router)
    with TestClient(app) as client:
        client.cookies.set(COOKIE, make_session("manager", cfg.session_secret, user_id="manager", version=1))
        path = f"/api/notifications/{notice_id}"
        assert client.get(path).status_code == 200
        assert opened == 1
        assert client.get(path).status_code == 401


def test_batch_completion_and_outbox_roll_back_together(notices_context, monkeypatch):
    from app.models import Batch, Notice
    from app.services import notifications
    from app.services.execution import PricingService

    factory, cfg = notices_context
    cfg.coordination_required = True
    service = PricingService(factory, None, cfg)
    write = notifications.write_batch_notices

    def interrupt(session, batch_id, **options):
        assert write(session, batch_id, **options) == 1
        raise RuntimeError("simulate interruption before commit")

    with monkeypatch.context() as patch:
        patch.setattr(notifications, "write_batch_notices", interrupt)
        with pytest.raises(RuntimeError, match="simulate interruption"):
            service._summarize("b")
    with factory() as session:
        assert session.get(Batch, "b").status == "done"
        assert session.scalar(select(Notice)) is None

    service._summarize("b")
    service._summarize("b")
    with factory() as session:
        assert session.get(Batch, "b").status == "completed"
        assert session.scalars(select(Notice)).one().delivery_state == "pending"
