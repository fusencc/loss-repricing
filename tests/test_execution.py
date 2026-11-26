from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal
import pytest
from sqlalchemy import select
from app.db import Base, create_database
from app.config import Settings
from app.models import Item, Execution, VariantGuard
from fake_adapter import DemoAdapter
from app.adapters.base import LookupResult, SubmitResult
from app.services.execution import PricingService
from app.services.store import update_settings, settings_value


@pytest.fixture
def context(tmp_path):
    engine, factory = create_database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    # 本组独立验证既有执行协议；销售权限在test_coordination_execution中覆盖。
    cfg = Settings(runtime_dir=tmp_path, coordination_required=False, _env_file=None)
    yield factory, cfg, DemoAdapter(tmp_path)
    engine.dispose()


def enable(factory):
    update_settings(factory, dict(autonomous_enabled=True, pilot_shops=[f"DEMO-{s}" for s in ["MY","PH","SG","TH","VN"]],
                                  site_enabled=["MY","PH","SG","TH","VN"]))


def test_rerun_keeps_unobserved_candidate_pending_if_a_row_could_not_be_located(context):
    from app.models import Batch

    factory, cfg, _ = context

    class ChangedSource(DemoAdapter):
        changed = False

        def collect(self, day):
            snapshot = super().collect(day)
            if self.changed:
                snapshot.unlocated_candidate_rows = True
                snapshot.lines = [line for line in snapshot.lines if line.key.site != "MY"]
            return snapshot

    adapter = ChangedSource(cfg.runtime_dir)
    service = PricingService(factory, adapter, cfg)
    batch_id = service.run_batch('partial-rerun')
    adapter.changed = True
    assert service.run_batch('partial-rerun') == batch_id

    with factory() as session:
        items = list(session.scalars(select(Item).where(Item.batch_id == batch_id)))
        missing = [item for item in items if item.key['site'] == 'MY']
        assert missing and all(item.phase == 'blocked' and item.status == '待处理' for item in missing)
        assert any(item.key['site'] == 'PH' and item.decision['ready'] for item in items)
        assert session.get(Batch, batch_id).error is None
        assert session.scalar(select(Execution.id)) is None


@pytest.mark.parametrize('overshoot,expected',[('0','verified'),('.001','mismatch')])
def test_readback_above_reference_is_not_verified_within_old_amount_tolerance(context,overshoot,expected):
    from test_sku_price_reference import ceiling
    factory,cfg,_=context
    class Capped(DemoAdapter):
        def collect(self,day):
            snapshot=super().collect(day)
            snapshot.online=snapshot.online[:1]
            snapshot.lines=snapshot.lines[:2]
            snapshot.online[0].sku_price_reference=ceiling('10.8')
            return snapshot
        def lookup(self,*args):
            result=super().lookup(*args)
            return replace(result,actual_price=result.actual_price+Decimal(overshoot))
    enable(factory)
    service = PricingService(factory, Capped(cfg.runtime_dir), cfg)
    service.run_batch('reference-readback')
    service.reconcile()
    with factory() as session:
        execution=session.scalar(select(Execution))
        assert execution.state==expected
        if overshoot!='0':
            assert execution.evidence['price_limit_passed'] is False
            assert session.get(VariantGuard,execution.variant_id).execution_id==execution.id


def test_submission_refresh_uses_the_new_lower_reference_cap(context):
    from test_sku_price_reference import ceiling
    factory,cfg,_=context
    class ChangingCap(DemoAdapter):
        collections=0
        def collect(self,day):
            self.collections+=1
            snapshot=super().collect(day)
            snapshot.online=snapshot.online[:1];snapshot.lines=snapshot.lines[:2]
            snapshot.online[0].sku_price_reference=ceiling('10.9' if self.collections==1 else '10.8')
            return snapshot
    enable(factory)
    PricingService(factory,ChangingCap(cfg.runtime_dir),cfg).run_batch('refresh-reference')
    with factory() as session:
        execution=session.scalar(select(Execution))
        assert Decimal(execution.request['upload_price'])==Decimal('21.60')
        assert execution.request['trace']['price_limit']['upper_actual']=='10.8'


@pytest.mark.parametrize('cap',['9','10','10.6'])
def test_reference_priority_can_submit_and_verify_even_with_loss_or_no_price_increase(context,cap):
    from test_sku_price_reference import ceiling
    factory,cfg,_=context
    class ReferencePriority(DemoAdapter):
        def collect(self,day):
            snapshot=super().collect(day)
            snapshot.online=snapshot.online[:1]
            snapshot.lines=snapshot.lines[:2]
            snapshot.online[0].sku_price_reference=ceiling(cap)
            return snapshot
    enable(factory)
    service = PricingService(factory, ReferencePriority(cfg.runtime_dir), cfg)
    service.run_batch('reference-priority')
    service.reconcile()
    with factory() as session:
        execution=session.scalar(select(Execution))
        assert execution is not None and execution.state=='verified'
        assert Decimal(execution.request['upload_price'])==Decimal(cap)*2
        assert execution.evidence['online_margin_passed'] is False
        assert execution.evidence['minimum_margin_required'] is False
        assert execution.evidence['price_limit_passed'] is True
        assert session.get(VariantGuard,execution.variant_id).execution_id is None
    assert settings_value(factory)['paused_sites']==[]


def test_duplicate_online_references_keep_one_profit_source_for_readback(context):
    from test_sku_price_reference import ceiling
    factory,cfg,_=context
    class DuplicateReference(DemoAdapter):
        def collect(self,day):
            snapshot=super().collect(day)
            first=snapshot.online[0]
            snapshot.online=[first.model_copy(update={'sku_price_reference':ceiling('10.9')}),
                             first.model_copy(update={'sku_price_reference':ceiling('10.8')})]
            snapshot.lines=snapshot.lines[:2]
            return snapshot
    enable(factory)
    service = PricingService(factory, DuplicateReference(cfg.runtime_dir), cfg)
    service.run_batch('duplicate-reference')
    service.reconcile()
    with factory() as session:
        execution=session.scalar(select(Execution))
        assert execution.state=='verified'
        assert Decimal(execution.request['upload_price'])==Decimal('21.60')
        assert len(execution.request['trace']['online_rows'])==1
        assert execution.request['trace']['online_rows'][0]['sku_price_reference']['marketing_price']=='10.8'


def test_batch_submits_all_items_before_background_readback(context):
    factory, cfg, _ = context

    class TrackedAdapter(DemoAdapter):
        lookups = 0

        def lookup(self, *args):
            self.lookups += 1
            return super().lookup(*args)

    adapter = TrackedAdapter(cfg.runtime_dir)
    service = PricingService(factory, adapter, cfg)
    assert isinstance(service.run_batch('initial'), str)
    with factory() as session:
        assert session.query(Execution).count() == 0
    enable(factory)
    batch_id = service.run_batch('enabled')
    assert adapter.lookups == 0
    with factory() as session:
        pending = session.scalars(select(Execution)).all()
        assert len(pending) >= 2
        assert all(execution.state == 'accepted' for execution in pending)
        assert all(session.get(VariantGuard, execution.variant_id).execution_id == execution.id
                   for execution in pending)
    service.reconcile()
    with factory() as session:
        verified = session.scalars(select(Item).where(Item.batch_id == batch_id, Item.status == '已核验')).all()
        assert len(verified) == len(pending)
        assert all(session.scalar(select(Execution).where(Execution.item_id == item.id)).evidence for item in verified)


def test_unknown_after_external_acceptance_recovers_without_resubmit(context):
    factory, cfg, adapter = context
    class TimeoutAfterAccept(DemoAdapter):
        calls = 0
        blocked = True
        def submit(self, execution_id, decision):
            self.calls += 1
            super().submit(execution_id, decision)
            raise TimeoutError("secret-bearing-url must never be stored")
        def lookup(self, *args):
            return LookupResult("unknown") if self.blocked else super().lookup(*args)
    source = TimeoutAfterAccept(cfg.runtime_dir)
    enable(factory)
    PricingService(factory, source, cfg).run_batch("first")
    original_calls = source.calls
    assert original_calls >= 2
    PricingService(factory, source, cfg).run_batch("second")
    assert source.calls == original_calls
    source.blocked = False
    restarted = PricingService(factory, source, cfg)
    restarted.reconcile()
    with factory() as session:
        executions = session.scalars(select(Execution)).all()
        assert all(e.state == "verified" for e in executions)
        assert all("secret-bearing" not in str(e.response) for e in executions)
    restarted.run_batch("third")
    assert source.calls == original_calls


def test_three_price_mismatches_keep_guards_without_pausing_site(context):
    factory, cfg, adapter = context
    class BadPrice(DemoAdapter):
        def lookup(self, execution_id, task_id, key):
            result = super().lookup(execution_id, task_id, key)
            if key.site == "MY" and result.actual_price is not None:
                return replace(result, actual_price=result.actual_price + Decimal("50"))
            return result

    enable(factory)
    service = PricingService(factory, BadPrice(cfg.runtime_dir), cfg)
    service.run_batch('bad-price')
    service.reconcile()
    assert settings_value(factory)["paused_sites"] == []
    with factory() as session:
        items = session.scalars(select(Item)).all()
        assert any(item.key["site"] != "MY" and item.status == "已核验" for item in items)
        mismatched = [item for item in items if item.phase == "mismatch"]
        assert len(mismatched) == 3
        for item in mismatched:
            assert item.key["site"] == "MY" and item.status == "待核验"
            execution = session.scalar(select(Execution).where(Execution.item_id == item.id))
            assert session.get(VariantGuard, execution.variant_id).execution_id == execution.id


def test_paused_submissions_still_reconcile_existing_results(context):
    factory, cfg, adapter = context
    class Delayed(DemoAdapter):
        waiting = True
        def lookup(self, *args):
            return LookupResult("unknown") if self.waiting else super().lookup(*args)
    source = Delayed(cfg.runtime_dir)
    enable(factory)
    service = PricingService(factory, source, cfg)
    service.run_batch("delayed")
    update_settings(factory, {"paused": True})
    source.waiting = False
    service.reconcile()
    with factory() as session:
        assert session.query(Execution).filter(Execution.state == "verified").count() >= 2
