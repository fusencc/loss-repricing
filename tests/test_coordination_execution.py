"""销售协作关键回归：全部使用隔离数据库和合成来源，不访问真实系统。"""
from decimal import Decimal
from sqlalchemy import select
import pytest
from app.config import Settings
from app.db import Base, create_database, transaction
from app.models import Item, Execution, VariantGuard
from app.models.coordination import TargetControl
from app.adapters.demo import DemoAdapter
from app.adapters.base import LookupResult
from app.services.execution import PricingService
from app.services.coordination import ensure_demo_ownership
from app.services.store import update_settings, utcnow, BEIJING

class OneSku(DemoAdapter):
    calls = 0
    collections = 0
    changed = False
    unknown = False
    def collect(self, day):
        self.collections += 1
        snapshot = super().collect(day)
        snapshot.online = snapshot.online[:1]
        snapshot.lines = snapshot.lines[:2]
        if self.changed and self.collections >= 2:
            snapshot.online[0].retail_price = Decimal("24")
            snapshot.online[0].marketing_price = Decimal("12")
        return snapshot
    def submit(self, *args):
        self.calls += 1
        return super().submit(*args)
    def lookup(self, *args):
        return LookupResult("unknown") if self.unknown else super().lookup(*args)

@pytest.fixture
def world(tmp_path):
    engine, factory = create_database(f"sqlite:///{tmp_path/'db.sqlite'}")
    Base.metadata.create_all(engine)
    cfg = Settings(runtime_dir=tmp_path, coordination_required=True, _env_file=None)
    adapter = OneSku(tmp_path)
    key = adapter.collect(utcnow().astimezone(BEIJING).date()).online[0].key
    adapter.collections = 0
    update_settings(factory, dict(autonomous_enabled=True,pilot_shops=["DEMO-MY"],site_enabled=["MY"]))
    yield factory,cfg,adapter,key
    engine.dispose()

def test_missing_owner_prevents_automatic_submission(world):
    factory,cfg,adapter,key=world
    PricingService(factory,adapter,cfg).run_batch("unassigned")
    assert adapter.calls == 0
    with factory() as s:
        assert s.scalar(select(Item)).status == "待处理"

def test_changed_external_price_invalidates_old_plan_without_recalculating_increase(world):
    factory,cfg,adapter,key=world
    ensure_demo_ownership(factory,[key])
    adapter.changed=True
    PricingService(factory,adapter,cfg).run_batch("changed")
    assert adapter.calls == 0
    with factory() as s:
        assert s.get(TargetControl,key.target_identity()).mode == "review"
        assert "外部" in s.scalar(select(Item)).reason

def test_takeover_after_claim_cancels_provably_unsent_intent(world):
    factory,cfg,adapter,key=world
    ensure_demo_ownership(factory,[key])
    class Takeover(PricingService):
        def _claim(self,*args,**kwargs):
            eid=super()._claim(*args,**kwargs)
            if eid:
                with transaction(self.factory) as s:
                    row=s.get(TargetControl,key.target_identity())
                    row.mode="manual"
                    row.version+=1
            return eid
    Takeover(factory,adapter,cfg).run_batch("takeover")
    assert adapter.calls == 0
    with factory() as s:
        assert s.get(VariantGuard,key.target_identity()).execution_id is None
        assert s.scalar(select(Execution)).response["status"] == "not_submitted"

def test_takeover_does_not_release_unknown_or_stop_reconciliation(world):
    factory,cfg,adapter,key=world
    ensure_demo_ownership(factory,[key])
    adapter.unknown=True
    service=PricingService(factory,adapter,cfg)
    service.run_batch("unknown")
    assert adapter.calls == 1
    with transaction(factory) as s:
        row=s.get(TargetControl,key.target_identity())
        row.mode="manual"; row.version+=1
    service.run_batch("again")
    assert adapter.calls == 1
    adapter.unknown=False
    service.reconcile()
    with factory() as s:
        assert s.scalar(select(Execution)).state == "verified"
        assert s.get(TargetControl,key.target_identity()).mode == "manual"

def test_live_without_exclusive_writer_contract_remains_closed(world,monkeypatch):
    factory,cfg,adapter,key=world
    ensure_demo_ownership(factory,[key])
    adapter.mode="live"
    from test_user_pricing_policy import mock_official_zero
    mock_official_zero(monkeypatch,[utcnow()])
    adapter.readiness=lambda: {"contract_ready":True,"verified_sites":["MY"]}
    cfg.live_submit_enabled=True
    PricingService(factory,adapter,cfg).run_batch("live")
    assert adapter.calls == 0

def test_manual_price_keeps_profit_and_rounding_checks(world):
    from app.services.corrections import manual_decision
    factory,cfg,adapter,key=world
    snapshot=adapter.collect(utcnow().astimezone(BEIJING).date())
    accepted=manual_decision(snapshot,snapshot.online[0],Decimal("11"),"reason","c1",utcnow())
    assert accepted.upload_price == Decimal("22")
    with pytest.raises(ValueError,match="利润率"):
        manual_decision(snapshot,snapshot.online[0],Decimal("10"),"reason","c1",utcnow())

@pytest.mark.parametrize(("identity_changes", "blocked"), [
    ({"item_id": "SIBLING"}, False),
    ({"sku": "ALIAS"}, True),
    ({"shop_id": "OTHER", "item_id": "SIBLING"}, False),
])
def test_manual_price_field_issues_only_hold_same_target(world, identity_changes, blocked):
    from app.domain.types import OnlineSourceIssue
    from app.services.corrections import manual_decision

    _, _, adapter, key = world
    now = utcnow()
    snapshot = adapter.collect(now.astimezone(BEIJING).date())
    online = snapshot.online[0]
    snapshot.source_issues.append(OnlineSourceIssue(
        source="online", record_number=2, key=key.model_copy(update=identity_changes),
        main_sku=online.main_sku,
    ))
    if blocked:
        with pytest.raises(ValueError, match="在线数据异常"):
            manual_decision(snapshot, online, Decimal("11"), "reason", "c1", utcnow())
    else:
        assert manual_decision(snapshot, online, Decimal("11"), "reason", "c1", utcnow()).ready



def test_manual_correction_ignores_reference_sales_differences(world):
    from app.services.corrections import manual_decision

    _, _, adapter, key = world
    now = utcnow()
    snapshot = adapter.collect(now.astimezone(BEIJING).date())
    online = snapshot.online[0]
    sibling = key.model_copy(update={"item_id": "SIBLING"})
    snapshot.online.append(online.model_copy(update={"key": sibling}))

    assert manual_decision(snapshot, online, Decimal("11"), "reason", "c1", utcnow()).ready


def queued_correction(world):
    from app.models.coordination import Correction
    from app.services.store import stamp
    from app.services.coordination import baseline_from_item
    factory,cfg,adapter,key=world
    ensure_demo_ownership(factory,[key])
    update_settings(factory,dict(autonomous_enabled=False))
    service=PricingService(factory,adapter,cfg)
    service.run_batch("proposal")
    with transaction(factory) as s:
        item=s.scalar(select(Item))
        control=s.get(TargetControl,key.target_identity())
        control.mode="manual";control.version+=1
        s.add(Correction(id="c1",target_id=key.target_identity(),item_id=item.id,desired_actual="11",
                         reason="修正价格",requested_by="admin",approved_by="admin",state="queued",
                         control_version=control.version,baseline=baseline_from_item(item),
                         created_at=stamp(),updated_at=stamp(),result={}))
    return service

def test_correction_runs_once_and_remains_manual(world):
    from app.services.corrections import run_correction
    from app.models.coordination import Correction
    service=queued_correction(world)
    factory,cfg,adapter,key=world
    batch_id = run_correction(service, "c1")
    assert batch_id
    assert run_correction(service, "c1") == batch_id
    assert adapter.calls == 1
    with factory() as s:
        assert s.get(Correction,"c1").state == "verified"
        assert s.get(TargetControl,key.target_identity()).mode == "manual"

def test_rejected_correction_recovers_after_interruption(world, monkeypatch):
    from app.adapters.base import SubmitResult
    from app.models import Batch, Notice
    from app.models.coordination import Correction
    from app.services.corrections import run_correction

    service = queued_correction(world)
    factory, _, adapter, _ = world
    sent = []

    def reject(execution_id, decision):
        sent.append(execution_id)
        return SubmitResult("rejected", message="模拟明确拒绝")

    def interrupt(**kwargs):
        raise RuntimeError("模拟进程中断")

    monkeypatch.setattr(adapter, "submit", reject)
    with monkeypatch.context() as patch:
        patch.setattr(service, "reconcile", interrupt)
        with pytest.raises(RuntimeError, match="模拟进程中断"):
            run_correction(service, "c1")

    with factory() as session:
        assert session.get(Correction, "c1").state == "executing"
        assert session.get(Execution, sent[0]).state == "failed"

    batch_id = run_correction(service, "c1")
    with factory() as session:
        correction = session.get(Correction, "c1")
        assert correction.state == "failed" and correction.result["message"] == "模拟明确拒绝"
        assert session.get(Batch, batch_id).status == "completed"
        notice = session.scalars(select(Notice).where(Notice.batch_id == batch_id)).one()
        assert notice.summary["statuses"] == {"待处理": 1}
    assert sent == [correction.execution_id]


def test_correction_cannot_execute_from_another_batch_mode(world):
    from app.models import Batch
    from app.services.corrections import run_correction

    service = queued_correction(world)
    factory, _, adapter, _ = world
    with transaction(factory) as session:
        session.scalars(select(Batch)).one().mode = "live"
    previous_collections = adapter.collections

    with pytest.raises(ValueError, match="修正来源批次模式不一致"):
        run_correction(service, "c1")
    assert adapter.collections == previous_collections
    assert adapter.calls == 0
    with factory() as session:
        assert session.scalars(select(Execution)).first() is None


def test_correction_refuses_stale_price(world):
    from app.services.corrections import run_correction
    from app.models.coordination import Correction
    service=queued_correction(world)
    factory,cfg,adapter,key=world
    adapter.changed=True
    run_correction(service,"c1")
    assert adapter.calls == 0
    with factory() as s:
        assert s.get(Correction,"c1").state == "stale"

def test_handback_after_refresh_does_not_revive_old_intent(world):
    factory,cfg,adapter,key=world
    ensure_demo_ownership(factory,[key])
    class Handback(PricingService):
        changed=False
        def _claim(self,*args,**kwargs):
            if not self.changed:
                with transaction(factory) as s:
                    c=s.get(TargetControl,key.target_identity());c.mode='auto';c.version+=2;c.handback_pending=True
                self.changed=True
            return super()._claim(*args,**kwargs)
    Handback(factory,adapter,cfg).run_batch('handback-race')
    assert adapter.calls==0
    # Even after another collector establishes the new baseline, the original authorization is stale.
    with factory() as s:
        old=s.scalar(select(Item)).decision
    from app.services.price_control import observe_prices
    from app.domain.types import Decision
    observe_prices(factory,adapter.collect(utcnow().astimezone(BEIJING).date()))
    with transaction(factory) as s:
        from app.services.coordination import automation_gate
        assert automation_gate(s,key,Decision.model_validate(old).trace.get('authorization'))[0] is False

def test_final_send_locks_scope_missing_override_owner_before_guard(world):
    from sqlalchemy import event
    from sqlalchemy.dialects import mysql
    from app.models.coordination import UserAccount,Ownership
    from app.services.store import stamp
    factory,cfg,adapter,key=world
    ensure_demo_ownership(factory,[key])
    with transaction(factory) as s:
        s.add(UserAccount(id='sales',username='sales',display_name='Sales',role='sales',active=True,can_correct=True,password_hash='unused',session_version=1,created_at=stamp(),updated_at=stamp()))
        s.scalar(select(Ownership)).owner_id='sales'
    statements=[]
    def record(state):statements.append(str(state.statement.compile(dialect=mysql.dialect())))
    event.listen(factory.class_,'do_orm_execute',record)
    old_submit=adapter.submit
    def submit(*args):
        # At the external boundary, the transaction retains all row locks. Scope sentinel
        # serializes creation of an absent target override; no reliance on SQLite gap locks.
        locks=[q for q in statements if 'FOR UPDATE' in q]
        assert any('settings' in q and 'settings.`key`' in q for q in locks)
        assert any('ownerships' in q for q in locks)
        assert any('user_accounts' in q for q in locks)
        last_control=max(i for i,q in enumerate(statements) if 'FOR UPDATE' in q and 'target_controls' in q)
        tail=statements[last_control:]
        scope=next(i for i,q in enumerate(tail) if 'FOR UPDATE' in q and 'settings' in q)
        own=next(i for i,q in enumerate(tail) if 'FOR UPDATE' in q and 'ownerships' in q)
        user=next(i for i,q in enumerate(tail) if 'FOR UPDATE' in q and 'user_accounts' in q)
        assert 0<scope<own<user
        return old_submit(*args)
    adapter.submit=submit
    try:PricingService(factory,adapter,cfg).run_batch('mysql-lock-contract')
    finally:event.remove(factory.class_,'do_orm_execute',record)
    assert adapter.calls==1

def test_ownership_version_change_after_refresh_invalidates_intent(world):
    from app.models.coordination import Ownership
    factory,cfg,adapter,key=world
    ensure_demo_ownership(factory,[key])
    class Transfer(PricingService):
        def _claim(self,*args,**kwargs):
            with transaction(factory) as s:s.scalar(select(Ownership)).version+=1
            return super()._claim(*args,**kwargs)
    Transfer(factory,adapter,cfg).run_batch('transfer')
    assert adapter.calls==0

def test_owner_disabled_after_claim_prevents_external_send(world):
    from app.models.coordination import UserAccount,Ownership
    from app.services.store import stamp
    factory,cfg,adapter,key=world
    ensure_demo_ownership(factory,[key])
    with transaction(factory) as s:
        s.add(UserAccount(id='sales',username='sales',display_name='Sales',role='sales',active=True,can_correct=True,password_hash='unused',session_version=1,created_at=stamp(),updated_at=stamp()))
        s.scalar(select(Ownership)).owner_id='sales'
    class Disable(PricingService):
        def _claim(self,*args,**kwargs):
            ident=super()._claim(*args,**kwargs)
            with transaction(factory) as s:s.get(UserAccount,'sales').active=False
            return ident
    Disable(factory,adapter,cfg).run_batch('disable-boundary')
    assert adapter.calls==0
    with factory() as s:assert s.scalar(select(Execution)).response['status']=='not_submitted'


def test_locked_authorization_never_returns_to_repeatable_read_snapshot(world, monkeypatch):
    """模拟MySQL同事务普通SELECT只能看到旧快照，locking read仍看到新override。"""
    from app.models.coordination import Ownership, UserAccount
    import app.services.coordination as coordination
    from app.services.price_control import lock_control
    from app.services.store import stamp
    factory, _, _, key = world
    ensure_demo_ownership(factory, [key])
    with transaction(factory) as s:
        old = s.scalar(select(Ownership))
        old_authorization = {'control_version': 1, 'ownership_id': old.id, 'ownership_version': old.version}
        s.add(UserAccount(id='new-owner', username='new-owner', display_name='新负责人', role='sales',
            active=True, can_correct=True, password_hash='unused', session_version=1,
            created_at=stamp(), updated_at=stamp()))
        s.add(Ownership(id='override', scope_key=coordination.scope_key(key.platform,key.shop_id,key.site,key.target_identity()),
            platform=key.platform,shop_id=key.shop_id,site=key.site,target_id=key.target_identity(),
            owner_id='new-owner',autonomous=True,version=1))
    with transaction(factory) as s:
        lock_control(s,key)
        locked = coordination.lock_authorization(s,key)
        # A regression must not use ordinary snapshot reads, including session.get(UserAccount).
        def stale_read(*args, **kwargs):
            raise AssertionError('locked gate used a potentially stale consistent read')
        monkeypatch.setattr(coordination, 'resolve_ownership', stale_read)
        get = s.get
        def guarded_get(model, *args, **kwargs):
            if model is UserAccount:
                stale_read()
            return get(model, *args, **kwargs)
        monkeypatch.setattr(s, 'get', guarded_get)
        assert coordination.authorization_snapshot(s,key,locked=locked)['ownership_id']=='override'
        assert not coordination.automation_gate(s,key,old_authorization,locked=locked)[0]
        current = coordination.authorization_snapshot(s,key,locked=locked)
        assert coordination.automation_gate(s,key,current,locked=locked)[0]


def test_correction_uses_locked_current_user_and_scope(world, monkeypatch):
    from app.models.coordination import Correction, Ownership, UserAccount
    from app.services.coordination import lock_authorization
    from app.services.corrections import correction_gate
    from app.services.price_control import lock_control
    from app.services.store import stamp
    import app.services.coordination as coordination
    from types import SimpleNamespace
    factory, _, _, key = world
    queued_correction(world)
    with transaction(factory) as s:
        s.add(UserAccount(id='sales',username='sales',display_name='Sales',role='sales',active=True,
            can_correct=False,password_hash='unused',session_version=1,created_at=stamp(),updated_at=stamp()))
        s.scalar(select(Ownership)).owner_id='sales'
        correction=s.get(Correction,'c1')
        correction.requested_by=correction.approved_by='sales'
    with transaction(factory) as s:
        control=lock_control(s,key)
        locked=lock_authorization(s,key,['sales'])
        def stale_read(*args, **kwargs):
            raise AssertionError('correction gate read old authorization snapshot')
        monkeypatch.setattr(coordination,'resolve_ownership',stale_read)
        get=s.get
        def guarded_get(model,*args,**kwargs):
            if model is UserAccount:stale_read()
            return get(model,*args,**kwargs)
        monkeypatch.setattr(s,'get',guarded_get)
        decision=SimpleNamespace(key=key,trace={'correction_id':'c1'})
        allowed,reason=correction_gate(s,decision,control,locked=locked)
        assert not allowed and '权限已撤销' in reason


@pytest.mark.parametrize('pending',[False,True])
def test_live_manual_correction_exempts_positive_official_count_but_keeps_pending_lock(world,monkeypatch,pending):
    from datetime import timedelta
    from app.services.corrections import run_correction
    factory,cfg,adapter,key=world
    adapter.mode='live'
    cfg.live_submit_enabled=True
    adapter.readiness=lambda:{'contract_ready':True,'verified_sites':['MY'],'manual_targets':[key.target_identity()]}
    def official(settings,keys,**kwargs):
        now=utcnow()
        return {'source_id':'synthetic-official','window_start':(now-timedelta(hours=72)).isoformat(),
            'window_end':now.isoformat(),'records':{k.item_id:{'status':'ok','count':3} for k in keys}}
    monkeypatch.setattr('app.services.execution.capture_recent_price_changes',official)
    service=queued_correction(world)
    if pending:
        with transaction(factory) as session:
            session.add(VariantGuard(variant_id=key.target_identity(),execution_id='pending-other'))
    run_correction(service,'c1')
    assert adapter.calls==(0 if pending else 1)
