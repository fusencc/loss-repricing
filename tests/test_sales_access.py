import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app

@pytest.fixture
def client(tmp_path):
    c=Settings(database_url=f"sqlite:///{tmp_path/'sales.db'}",runtime_dir=tmp_path,admin_password='test-local-password',session_secret='test-session-secret-32-characters-long',_env_file=None)
    with TestClient(create_app(c)) as x: yield x

def admin(c):
    assert c.post('/api/auth/login',json={'username':'admin','password':'test-local-password'}).status_code==200

def test_sales_scope_and_password_revocation(client):
    admin(client)
    created=client.post('/api/users',json={'username':'alice','display_name':'Alice','password':'strong-password','role':'sales'})
    assert created.status_code==200
    u=created.json(); assert 'password_hash' not in u
    assert client.post('/api/auth/login',json={'username':'alice','password':'strong-password'}).status_code==200
    token=client.cookies.get('lr_session')
    assert client.get('/api/auth/me').json()['role']=='sales'
    assert client.get('/api/users').status_code==403
    assert client.post('/api/batches/run').status_code==403
    assert client.get('/api/overview').json()['batch_count']==0
    admin(client)
    assert client.patch('/api/users/'+u['id'],json={'password':'replacement-password'}).status_code==200
    client.cookies.set('lr_session',token)
    assert client.get('/api/auth/me').status_code==401

def test_reserved_admin_and_ownership_version(client):
    admin(client)
    assert client.post('/api/users',json={'username':'admin','display_name':'Bad','password':'strong-password','role':'sales'}).status_code==409
    r=client.post('/api/ownerships',json={'platform':'demo','shop_id':'s','site':'MY','owner_id':'admin','autonomous':True})
    assert r.status_code==200
    o=r.json()
    assert client.patch('/api/ownerships/'+o['id'],json={'version':o['version'],'autonomous':False}).status_code==200
    assert client.patch('/api/ownerships/'+o['id'],json={'version':o['version'],'autonomous':True}).status_code==409

def seed_items(client):
    from app.models import Batch,Item,Job
    from app.db import transaction
    from app.services.store import stamp
    from app.domain.types import VariantKey
    factory=client.app.state.factory
    with transaction(factory) as s:
        s.add(Batch(id='b',run_key='b',business_date='2026-11-05',mode='demo',status='done',created_at=stamp(),snapshot={},summary={'private':99}))
        for ident,shop in [('one','s1'),('two','s2')]:
            key=dict(platform='demo',shop_id=shop,site='MY',product_id='p',item_id=ident,sku=ident,warehouse='w')
            online=dict(key=key,retail_price='10',marketing_price=None,current_margin='-1',currency='MYR',fee_scope='normal',observed_at=stamp(),main_sku='m')
            s.add(Item(id=ident,batch_id='b',variant_id=VariantKey.model_validate(key).identity(),key=key,status='待处理',phase='blocked',reason='x',decision={'trace':{'online_rows':[online]}},updated_at=stamp()))
        s.add(Job(id='private-job',dedupe_key='j',kind='run',state='queued',payload={'item_id':'two'},batch_id='b',lease_until=0,created_at=stamp()))
    original_collect=client.app.state.adapter.collect
    def collect(day):
        snapshot=original_collect(day)
        from app.domain.types import VariantKey
        from decimal import Decimal
        template=snapshot.online[0]
        with factory() as s:
            snapshot.online=[template.model_copy(update={'key':VariantKey.model_validate(i.key),'retail_price':Decimal('10'),'marketing_price':None}) for i in s.scalars(__import__('sqlalchemy').select(Item))]
        return snapshot
    client.app.state.adapter.collect=collect
    return factory

def test_direct_scope_checks_and_control_correction_versions(client):
    admin(client);seed_items(client)
    alice=client.post('/api/users',json={'username':'alice','display_name':'Alice','password':'strong-password','can_correct':True}).json()
    manager=client.post('/api/users',json={'username':'manager','display_name':'M','password':'strong-password','role':'manager'}).json()
    own=client.post('/api/ownerships',json={'platform':'demo','shop_id':'s1','site':'MY','owner_id':alice['id'],'supervisor_id':manager['id']}).json()
    assert client.post('/api/auth/login',json={'username':'alice','password':'strong-password'}).status_code==200
    assert [i['id'] for i in client.get('/api/items').json()]==['one']
    assert client.get('/api/items/two').status_code==404
    assert client.get('/api/jobs/private-job').status_code==404
    b=client.get('/api/batches/b').json()
    assert len(b['items'])==1 and 'private' not in b['summary']
    assert client.post('/api/items/two/control',json={'action':'takeover','reason':'mine','version':0}).status_code==404
    r=client.post('/api/items/one/control',json={'action':'takeover','reason':'mine','version':0})
    assert r.status_code==200 and r.json()['version']==1
    assert client.post('/api/items/one/control',json={'action':'handback','reason':'done','version':0}).status_code==409
    r=client.post('/api/items/one/corrections',json={'desired_actual':'12.00','reason':'fix','version':1})
    assert r.status_code==200; c=r.json()
    assert c['desired_actual']=='12.00' and c['baseline']['online_rows'][0]['retail_price']=='10'
    assert client.post('/api/corrections/'+c['id']+'/approve',json={'version':1}).status_code==409
    a=client.post('/api/corrections/'+c['id']+'/approve',json={'version':2});assert a.status_code==200
    a2=client.post('/api/corrections/'+c['id']+'/approve',json={'version':2});assert a2.status_code==200 and a2.json()['job_id']==a.json()['job_id']
    admin(client)
    assert client.patch('/api/ownerships/'+own['id'],json={'version':1,'owner_id':'admin','supervisor_id':None}).status_code==200
    client.post('/api/auth/login',json={'username':'alice','password':'strong-password'})
    assert client.get('/api/items/one').status_code==404
    assert client.post('/api/items/one/control',json={'action':'pause','reason':'old link','version':2}).status_code==404
    client.post('/api/auth/login',json={'username':'manager','password':'strong-password'})
    assert client.get('/api/items').json()==[]

def test_disabled_account_and_override_scope(client):
    admin(client);factory=seed_items(client)
    alice=client.post('/api/users',json={'username':'alice','display_name':'A','password':'strong-password'}).json()
    manager=client.post('/api/users',json={'username':'manager','display_name':'M','password':'strong-password','role':'manager'}).json()
    assert client.post('/api/ownerships',json={'platform':'demo','shop_id':'s1','site':'MY','owner_id':alice['id'],'supervisor_id':manager['id'],'autonomous':True}).status_code==200
    client.post('/api/auth/login',json={'username':'manager','password':'strong-password'})
    assert [i['id'] for i in client.get('/api/items').json()]==['one']
    assert client.get('/api/items/two').status_code==404
    client.post('/api/auth/login',json={'username':'alice','password':'strong-password'});token=client.cookies.get('lr_session')
    admin(client);assert client.patch('/api/users/'+alice['id'],json={'active':False}).status_code==200
    client.cookies.set('lr_session',token);assert client.get('/api/auth/me').status_code==401
    assert client.post('/api/auth/login',json={'username':'alice','password':'strong-password'}).status_code==401

def test_control_preserves_unknown_guard_and_stale_baseline_rejected(client):
    from app.db import transaction
    from app.models import VariantGuard,Item
    from app.models.coordination import TargetControl
    from app.services.coordination import target_id
    from app.services.price_control import price_baseline
    admin(client);factory=seed_items(client)
    with transaction(factory) as s:
        item=s.get(Item,'one');ident=target_id(item.key)
        s.add(VariantGuard(variant_id=ident,execution_id='unknown-execution'))
    result=client.post('/api/items/one/control',json={'action':'takeover','reason':'inspect','version':0})
    assert result.status_code==200
    with transaction(factory) as s:
        assert s.get(VariantGuard,ident).execution_id=='unknown-execution'
        item=s.get(Item,'one');control=s.get(TargetControl,ident)
        control.baseline={**price_baseline(item.decision['trace']['online_rows'][0]),'retail_price':'11'}
    assert client.post('/api/items/one/corrections',json={'desired_actual':'12','reason':'fix','version':0}).status_code==409

def test_owned_protections_use_online_trace_and_names(client):
    from app.models import Protection
    from app.services.store import stamp
    from app.db import transaction
    admin(client);factory=seed_items(client)
    alice=client.post('/api/users',json={'username':'alice','display_name':'Alice','password':'strong-password'}).json()
    client.post('/api/ownerships',json={'platform':'demo','shop_id':'s1','site':'MY','owner_id':alice['id']})
    with transaction(factory) as s:
        for ident,shop in [('owned','s1'),('foreign','s2')]:s.add(Protection(id=ident,shop_id=shop,site='MY',main_sku='m',reason='protect',active=True,created_at=stamp()))
    client.post('/api/auth/login',json={'username':'alice','password':'strong-password'})
    assert [p['id'] for p in client.get('/api/protections').json()]==['owned']
    assert client.get('/api/items/one').json()['ownership']['owner_name']=='Alice'

def test_correction_after_verified_price_collects_fresh_baseline(client):
    from app.models import Item
    from app.models.coordination import TargetControl
    from app.services.coordination import target_id
    from app.services.store import stamp
    from app.db import transaction
    from decimal import Decimal
    admin(client);factory=seed_items(client)
    with transaction(factory) as s:
        item=s.get(Item,'one');ident=target_id(item.key)
        s.add(TargetControl(target_id=ident,key=item.key,mode='auto',version=1,reason='',changed_by='system',updated_at=stamp(),baseline={'retail_price':'12'},handback_pending=False))
    collect=client.app.state.adapter.collect
    def fresh(day):
        snap=collect(day);snap.online[0].retail_price=Decimal('12');return snap
    client.app.state.adapter.collect=fresh
    r=client.post('/api/items/one/corrections',json={'desired_actual':'13.00','reason':'new proposal','version':1})
    assert r.status_code==200
    c=r.json();assert c['baseline']['online_rows'][0]['retail_price']=='12'
    with factory() as s:assert s.get(Item,'one').decision['trace']['online_rows'][0]['retail_price']=='10'


def test_related_source_issues_do_not_reveal_another_targets_identity(client):
    from app.domain.types import VariantKey
    from app.models import Batch, Item

    admin(client)
    factory = seed_items(client)
    with factory.begin() as session:
        mine = session.get(Item, "one")
        other = session.get(Item, "two")
        other.key = {**other.key, "shop_id": mine.key["shop_id"]}
        other.variant_id = VariantKey.model_validate(other.key).identity()
        own_issue = {
            "source": "online", "record_number": 1, "key": {**mine.key, "warehouse": None}, "main_sku": "m",
        }
        related_issue = {
            "source": "online", "record_number": 3, "key": other.key, "main_sku": "m",
            "relation": "shared_main_sku",
        }
        mine.decision = {**mine.decision, "trace": {
            **mine.decision["trace"], "source_issues": [own_issue, related_issue],
        }}
        my_target = VariantKey.model_validate(mine.key).target_identity()
        batch = session.get(Batch, "b")
        batch.snapshot = {"source_issues": [own_issue, related_issue]}

    audit_path = "/api/batches/b/source-issues"
    assert client.get("/api/batches/b").json()["source_issue_count"] == 2
    assert client.get(audit_path, params={"offset": 1, "limit": 1}).json() == {
        "total": 2, "items": [related_issue],
    }
    assert client.get(audit_path, params={"offset": 2}).json() == {"total": 2, "items": []}
    assert client.get(audit_path, params={"limit": 101}).status_code == 422
    assert client.get("/api/batches/absent/source-issues").status_code == 404
    admin_item = client.get("/api/items/one").json()
    assert admin_item["decision"]["trace"]["source_issues"][1] == related_issue
    owner = client.post("/api/users", json={
        "username": "alice", "display_name": "A", "password": "strong-password",
    }).json()
    assert client.post("/api/ownerships", json={
        "platform": "demo", "shop_id": "s1", "site": "MY", "owner_id": owner["id"], "target_id": my_target,
    }).is_success
    assert client.post("/api/auth/login", json={"username": "alice", "password": "strong-password"}).is_success
    assert client.get("/api/items/two").status_code == 404
    assert client.get(audit_path).status_code == 403
    assert "source_issue_count" not in client.get("/api/batches/b").json()
    item = client.get("/api/items/one").json()
    assert item["decision"]["trace"]["source_issues"] == [
        own_issue, {"source": "online", "record_number": 3, "relation": "shared_main_sku"},
    ]
