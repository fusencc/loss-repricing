from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal
import pytest
from sqlalchemy import select
from app.db import Base, create_database
from app.config import Settings
from app.models import Item, Execution, VariantGuard
from app.adapters.demo import DemoAdapter
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


def test_empty_pilot_never_submits_then_enabled_batch_verifies(context):
    factory, cfg, adapter = context
    service = PricingService(factory, adapter, cfg)
    first = service.run_batch("initial")
    assert isinstance(first, str)
    with factory() as session:
        assert session.query(Execution).count() == 0
    enable(factory)
    second = service.run_batch("enabled")
    with factory() as session:
        verified = session.scalars(select(Item).where(Item.batch_id == second, Item.status == "已核验")).all()
        assert len(verified) >= 2
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


def test_price_mismatch_pauses_site_and_keeps_guard(context):
    factory, cfg, adapter = context
    class BadPrice(DemoAdapter):
        def lookup(self, *args):
            result = super().lookup(*args)
            return replace(result, actual_price=result.actual_price + Decimal("50")) if result.actual_price is not None else result
    enable(factory)
    PricingService(factory, BadPrice(cfg.runtime_dir), cfg).run_batch("bad-price")
    assert "MY" in settings_value(factory)["paused_sites"]
    with factory() as session:
        assert session.query(VariantGuard).filter(VariantGuard.execution_id.is_not(None)).count() > 0
        assert session.query(Item).filter(Item.status == "已核验").count() == 0


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
