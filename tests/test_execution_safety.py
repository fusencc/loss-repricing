"""执行安全回归：临时SQLite、纯合成来源，不读取凭据或调用真实接口。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.adapters.base import LookupResult, SubmitResult
from app.config import Settings
from app.db import Base, create_database
from app.domain.engine import evaluate
from app.domain.types import Decision, Snapshot
from app.models import Batch, Execution, Item, Protection, VariantGuard
from app.services.execution import PricingService
from app.services.store import BEIJING, parse_stamp, stamp, update_settings


class SyntheticAdapter:
    """三个同店同站独立目标；全部回执由测试显式指定。"""

    def __init__(self, clock, mode="demo"):
        self.clock, self.mode = clock, mode
        self.collections, self.submissions, self.lookups = [], [], []
        self.results = {}
        self.on_submit = None

    def collect(self, day):
        self.collections.append(day)
        lines, online = [], []
        for index in range(3):
            key = dict(platform="synthetic", shop_id="TEST-SHOP", site="MY",
                       product_id=f"P{index}", item_id=f"I{index}", sku=f"SKU{index}", warehouse="direct")
            lines.append(dict(row_id=f"R{index}", order_id=f"O{index}", source="loss_allocatable",
                              key=key, main_sku=f"MAIN{index}", quantity="1", loss_amount="-1",
                              currency="MYR", basis="unit"))
            online.append(dict(key=key, main_sku=f"MAIN{index}", currency="MYR", retail_price="20",
                               marketing_price="10", current_margin="-0.5", fee_scope="normal",
                               fee_model=dict(revenue_factor="0.9", fixed_cost="9.5",
                                              version="synthetic-fees-v1", verified=True),
                               observed_at=self.clock[0], sales_7d=6, history_known=True, sales_complete=True,
                               sales_start=day - timedelta(days=7), sales_end=day - timedelta(days=1)))
        return Snapshot.model_validate(dict(
            as_of=self.clock[0], business_date=day, fx_date=day, fx_rates={"CNY": "1", "MYR": "0.6"},
            lines=lines, online=online,
            complete_sources=["loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"],
            contracts={"MY": dict(site="MY", verified=True, price_step="0.01", rounding="ROUND_HALF_UP",
                                  upload_column="synthetic-price", version="synthetic-contract-v1", warehouses=["direct"])},
            source_versions={"erp": "synthetic-v1"}))

    def submit(self, execution_id, decision):
        self.submissions.append((execution_id, decision))
        if self.on_submit:
            self.on_submit(decision)
        return SubmitResult("unknown", message="合成未知提交，仅允许回读")

    def lookup(self, execution_id, task_id, key):
        self.lookups.append(execution_id)
        return self.results.get(execution_id, LookupResult("unknown"))


@pytest.fixture
def safety_context(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 5, 2, tzinfo=timezone.utc)]
    monkeypatch.setattr("app.services.execution.utcnow", lambda: clock[0])
    monkeypatch.setattr("app.services.store.utcnow", lambda: clock[0])
    engine, factory = create_database(f"sqlite:///{tmp_path / 'execution-safety.sqlite'}")
    Base.metadata.create_all(engine)
    cfg = Settings(runtime_dir=tmp_path, coordination_required=False, _env_file=None)
    update_settings(factory, dict(autonomous_enabled=True, pilot_shops=["TEST-SHOP"], site_enabled=["MY"]))
    adapter = SyntheticAdapter(clock)
    yield factory, cfg, adapter, clock
    engine.dispose()


def executions(factory):
    with factory() as session:
        result = session.scalars(select(Execution).order_by(Execution.created_at, Execution.id)).all()
        assert result, [(item.status, item.reason) for item in session.scalars(select(Item))]
        return result


def persist_protection(factory, *, main_sku="MAIN1", shop="TEST-SHOP", site="MY", active=True):
    with factory.begin() as session:
        session.add(Protection(id=str(uuid4()), shop_id=shop, site=site, main_sku=main_sku,
                               active=active, reason="合成人工保护", created_at=stamp()))


@pytest.mark.parametrize("invalid_effective", ["missing", "naive", "before_submission", "after_observation"])
def test_missing_valid_effective_time_starts_72_hours_at_first_confirmation(safety_context, invalid_effective):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("first")
    original = executions(factory)[0]
    submitted_at = parse_stamp(original.created_at)
    observed_at = submitted_at + timedelta(minutes=2)
    effective_at = {
        "missing": None,
        "naive": (submitted_at + timedelta(minutes=1)).replace(tzinfo=None),
        "before_submission": submitted_at - timedelta(seconds=1),
        "after_observation": observed_at + timedelta(seconds=1),
    }[invalid_effective]
    # 旧来源观察已经超过72小时，但此前一直未知，此次才第一次确认价格。
    clock[0] += timedelta(days=4)
    confirmed_at = clock[0]
    adapter.results[original.id] = LookupResult("accepted", Decimal("11"), observed_at, effective_at)
    service.reconcile(item_id=original.item_id)
    with factory() as session:
        result = session.get(Execution, original.id)
        assert result.state == "verified"
        assert result.evidence["observed_at"] == stamp(observed_at)
        assert result.evidence["confirmed_at"] == stamp(confirmed_at)
        assert result.evidence["effective_at"] == stamp(confirmed_at)
        assert result.evidence["effective_time_source"] == "confirmed_at"
        assert session.get(VariantGuard, original.variant_id).last_effective_at == stamp(confirmed_at)
        assert session.get(VariantGuard, original.variant_id).execution_id is None
        evidence = dict(result.evidence)
    clock[0] += timedelta(hours=72) - timedelta(seconds=1)
    service.reconcile()
    service.run_batch("before-72h")
    assert len(adapter.submissions) == 3
    with factory() as session:
        assert session.get(Execution, original.id).evidence == evidence
    clock[0] += timedelta(seconds=1)
    service.run_batch("at-72h")
    assert len(adapter.submissions) == 4
    assert adapter.submissions[-1][1].key.target_identity() == original.variant_id


def test_valid_precise_effective_time_is_preserved_with_confirmation_evidence(safety_context):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("precise-effective")
    original = executions(factory)[0]
    effective_at = clock[0] + timedelta(minutes=1)
    observed_at = clock[0] + timedelta(minutes=2)
    clock[0] += timedelta(days=4)
    adapter.results[original.id] = LookupResult("accepted", Decimal("11"), observed_at, effective_at)
    service.reconcile(item_id=original.item_id)
    with factory() as session:
        result = session.get(Execution, original.id)
        assert result.evidence["effective_at"] == stamp(effective_at)
        assert result.evidence["effective_time_source"] == "effective_at"
        assert result.evidence["observed_at"] == stamp(observed_at)
        assert result.evidence["confirmed_at"] == stamp(clock[0])
        assert session.get(VariantGuard, original.variant_id).last_effective_at == stamp(effective_at)
    service.run_batch("precise-already-72h")
    assert len(adapter.submissions) == 4


def test_unknown_keeps_every_lock_and_never_retransmits(safety_context):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("unknown")
    original = executions(factory)
    clock[0] += timedelta(days=4)
    service.reconcile()
    service.run_batch("still-unknown")
    assert len(adapter.submissions) == 3
    assert len(adapter.lookups) == 6
    with factory() as session:
        for execution in original:
            assert session.get(Execution, execution.id).state == "unknown"
            guard = session.get(VariantGuard, execution.variant_id)
            assert guard.execution_id == execution.id
            assert guard.last_effective_at is None


def test_mismatch_is_not_first_successful_confirmation(safety_context):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("mismatch")
    original = executions(factory)[0]
    observed_at = clock[0] + timedelta(minutes=1)
    clock[0] += timedelta(hours=1)
    adapter.results[original.id] = LookupResult("accepted", Decimal("12"), observed_at)
    service.reconcile(item_id=original.item_id)
    with factory() as session:
        assert session.get(Execution, original.id).state == "mismatch"
        assert "confirmed_at" not in session.get(Execution, original.id).evidence
        assert session.get(VariantGuard, original.variant_id).execution_id == original.id
    clock[0] += timedelta(days=4)
    adapter.results[original.id] = LookupResult("accepted", Decimal("11"), observed_at)
    service.reconcile(item_id=original.item_id)
    with factory() as session:
        result = session.get(Execution, original.id)
        assert result.state == "verified"
        assert result.evidence["confirmed_at"] == stamp(clock[0])
        assert result.evidence["effective_at"] == stamp(clock[0])


def test_protection_added_inside_cached_batch_blocks_only_matching_target(safety_context):
    factory, cfg, adapter, _ = safety_context

    def protect_after_first(decision):
        if decision.key.item_id == "I0":
            persist_protection(factory)
            # 相同主SKU但店铺/站点不同，或已经停用，都不保护另一个独立目标。
            persist_protection(factory, main_sku="MAIN2", shop="OTHER-SHOP")
            persist_protection(factory, main_sku="MAIN2", site="PH")
            persist_protection(factory, main_sku="MAIN2", active=False)

    adapter.on_submit = protect_after_first
    service = PricingService(factory, adapter, cfg)
    batch_id = service.run_batch("protect-during-submit")
    assert len(adapter.collections) == 2  # 首次取数+提交前整批刷新，没有逐目标重取。
    assert [decision.key.item_id for _, decision in adapter.submissions] == ["I0", "I2"]
    with factory() as session:
        items = session.scalars(select(Item).where(Item.batch_id == batch_id)).all()
        protected = next(item for item in items if item.key["item_id"] == "I1")
        assert protected.status == "本轮不调"
        assert protected.phase == "excluded"
        assert "提交前命中人工保护" in protected.reason
        assert session.scalar(select(Execution).where(Execution.item_id == protected.id)) is None
        key = Decision.model_validate(protected.decision).key
        assert session.get(VariantGuard, key.target_identity()).execution_id is None
        assert all(item.status == "待核验" for item in items if item.id != protected.id)
    service.run_batch("protection-still-active")
    assert len(adapter.submissions) == 2  # 其他目标保持未知锁，受保护目标仍不上传。


@pytest.mark.parametrize("bad_evidence", ["missing", "wrong_target", "conflicting_main_sku"])
def test_claim_without_matching_main_sku_evidence_holds_only_that_item(safety_context, bad_evidence):
    factory, cfg, adapter, clock = safety_context
    update_settings(factory, {"paused": True})
    service = PricingService(factory, adapter, cfg)
    batch_id = service.run_batch("missing-main-sku")
    decisions = evaluate(adapter.collect(clock[0].astimezone(BEIJING).date()), clock[0])
    decision = decisions[0]
    if bad_evidence == "missing":
        decision.trace.pop("online_rows")
    elif bad_evidence == "wrong_target":
        decision.trace["online_rows"][0]["key"]["item_id"] = "FOREIGN"
    else:
        other = dict(decision.trace["online_rows"][0], main_sku="CONFLICT")
        decision.trace["online_rows"].append(other)
    update_settings(factory, {"paused": False})
    with factory() as session:
        items = {item.key["item_id"]: item for item in session.scalars(select(Item).where(Item.batch_id == batch_id))}
    service._submit(items["I0"].id, decision)
    service._submit(items["I1"].id, decisions[1])
    assert [d.key.item_id for _, d in adapter.submissions] == ["I1"]
    with factory() as session:
        held = session.get(Item, items["I0"].id)
        assert held.status == "待处理"
        assert "无法核对提交前人工保护" in held.reason


@pytest.mark.parametrize("other_mode", ["file", "live"])
@pytest.mark.parametrize("pending_state", ["submitting", "accepted", "unknown", "mismatch"])
def test_reconcile_never_reads_or_changes_foreign_mode_executions(safety_context, other_mode, pending_state):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("mode-isolation")
    original = executions(factory)
    with factory.begin() as session:
        for execution in original:
            session.get(Execution, execution.id).state = pending_state
            session.get(Item, execution.item_id).phase = pending_state
    other = SyntheticAdapter(clock, mode=other_mode)
    other.results = {execution.id: LookupResult("rejected") for execution in original}
    other_service = PricingService(factory, other, cfg)
    other_service.reconcile()
    other_service.reconcile(item_id=original[0].item_id)
    other_service.reconcile(target_id=original[0].variant_id)
    assert other.lookups == []
    assert other.submissions == []
    with factory() as session:
        for execution in original:
            current = session.get(Execution, execution.id)
            assert current.state == pending_state
            assert current.evidence == {}
            assert current.response == execution.response
            assert session.get(Item, execution.item_id).phase == pending_state
            assert session.get(VariantGuard, execution.variant_id).execution_id == execution.id
    # 回到原始模式后仍可恢复核验，原锁并未被错误释放。
    adapter.results = {execution.id: LookupResult("accepted", Decimal("11"), clock[0]) for execution in original}
    service.reconcile()
    assert all(execution.state == "verified" for execution in executions(factory))


def test_mode_filter_continues_matching_batch_in_mixed_database(safety_context):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("mixed-mode")
    original = executions(factory)
    foreign = original[0]
    with factory.begin() as session:
        batch = Batch(id=str(uuid4()), run_key="foreign-file", business_date="2026-11-05", mode="file",
                      status="pending_verification", created_at=stamp(), snapshot={}, summary={"待核验": 1})
        session.add(batch)
        session.flush()
        session.get(Item, foreign.item_id).batch_id = batch.id
    adapter.lookups.clear()
    adapter.results = {execution.id: LookupResult("accepted", Decimal("11"), clock[0]) for execution in original}
    service.reconcile()
    assert set(adapter.lookups) == {execution.id for execution in original[1:]}
    with factory() as session:
        assert session.get(Execution, foreign.id).state == "unknown"
        assert session.get(VariantGuard, foreign.variant_id).execution_id == foreign.id
        assert all(session.get(Execution, execution.id).state == "verified" for execution in original[1:])


@pytest.mark.parametrize("selected", [False, True])
def test_run_rejects_existing_batch_or_selected_item_from_other_mode(safety_context, selected):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    batch_id = service.run_batch("original-mode")
    original = executions(factory)[0]
    with factory() as session:
        before = dict(session.get(Batch, batch_id).snapshot)
    other = SyntheticAdapter(clock, mode="file")
    other_service = PricingService(factory, other, cfg)
    with pytest.raises(ValueError, match="原始批次模式与当前适配器不一致"):
        other_service.run_batch("original-mode", item_id=original.item_id if selected else None)
    assert other.lookups == other.submissions == []
    with factory() as session:
        batch = session.get(Batch, batch_id)
        assert batch.mode == "demo"
        assert batch.snapshot == before
        assert session.get(Execution, original.id).state == "unknown"
        assert session.get(VariantGuard, original.variant_id).execution_id == original.id


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(("actual", "state"), [
    ("11.02", "verified"),
    ("11.020000000000000000000000000001", "mismatch"),
])
def test_verification_tolerance_ignores_global_decimal_rounding(safety_context, legacy, actual, state):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("exact-tolerance")
    original = executions(factory)[0]
    if legacy:
        with factory.begin() as session:
            row = session.get(Execution, original.id)
            request = dict(row.request)
            request["trace"] = dict(request["trace"])
            request["trace"].pop("exact_target_actual")
            row.request = request
    adapter.results[original.id] = LookupResult("accepted", Decimal(actual), clock[0])
    with localcontext() as context:
        context.prec = 6
        service.reconcile(item_id=original.item_id)
    with factory() as session:
        assert session.get(Execution, original.id).state == state
        assert (session.get(VariantGuard, original.variant_id).execution_id is None) == (state == "verified")


def test_verification_uses_exact_target_instead_of_saved_repeating_decimal(safety_context):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("repeating-target")
    original = executions(factory)[0]
    with factory.begin() as session:
        row = session.get(Execution, original.id)
        request = dict(row.request)
        request["target_actual"] = "1.3333333333333333333333333333333333333333333333333"
        request["trace"] = dict(request["trace"], exact_target_actual={"numerator": "4", "denominator": "3"})
        row.request = request
    # 有限小数严格超过4/3+0.02，不能因默认28位精度而误认为恰好0.02。
    adapter.results[original.id] = LookupResult(
        "accepted", Decimal("1.3533333333333333333333333333333333333333333333334"), clock[0])
    service.reconcile(item_id=original.item_id)
    with factory() as session:
        assert session.get(Execution, original.id).state == "mismatch"
        assert session.get(VariantGuard, original.variant_id).execution_id == original.id
