"""执行安全回归：临时SQLite、纯合成来源，不读取凭据或调用真实接口。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.adapters.base import LookupResult, SourceUnavailable, SubmitResult
from app.config import Settings
from app.db import Base, create_database
from app.domain.engine import evaluate
from app.domain.types import Decision, Snapshot
from app.models import Batch, Execution, Item, Job, Protection, VariantGuard
from app.services.execution import PricingService
from app.services.store import BEIJING, parse_stamp, settings_value, stamp, update_settings


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
                               marketing_price="10", fee_scope="normal",
                               profit_check=dict(overseas_stock="0", mode="direct", department="TEST",
                                                 inventory_observed_at=self.clock[0], source_ref="synthetic-inventory",
                                                 model=dict(revenue_factor="1", fixed_cost="0",
                                                            version="synthetic-online-v1", verified=True)),
                               observed_at=self.clock[0], sales_7d=6, history_known=True, sales_complete=True,
                               sales_basis="erp_goods_warehouse_seven_days_reference", sales_observed_at=self.clock[0]))
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


@pytest.mark.parametrize("wrong_target", [False, True])
def test_invalid_saved_execution_does_not_stop_other_lookups(safety_context, wrong_target):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("invalid-saved-execution")
    invalid, valid, _ = executions(factory)
    with factory.begin() as session:
        session.get(Execution, invalid.id).request = valid.request if wrong_target else {}
    clock[0] += timedelta(minutes=1)
    adapter.results[valid.id] = LookupResult("accepted", Decimal("11"), clock[0])

    adapter.results[invalid.id] = adapter.results[valid.id]
    adapter.lookups.clear()
    service.reconcile()
    assert invalid.id not in adapter.lookups
    with factory() as session:
        assert session.get(Execution, invalid.id).state == "unknown"
        assert session.get(VariantGuard, invalid.variant_id).execution_id == invalid.id
        assert session.get(Execution, valid.id).state == "verified"
        assert session.get(VariantGuard, valid.variant_id).execution_id is None


@pytest.mark.parametrize("bad_target", [None, {"numerator": "11", "denominator": "0"}])
def test_invalid_saved_price_does_not_stop_other_verifications(safety_context, bad_target):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("invalid-saved-price")
    invalid, valid, _ = executions(factory)
    request = dict(invalid.request)
    trace = dict(request["trace"])
    if bad_target is None:
        request["target_actual"] = None
        trace.pop("exact_target_actual")
    else:
        trace["exact_target_actual"] = bad_target
    with factory.begin() as session:
        session.get(Execution, invalid.id).request = {**request, "trace": trace}
    clock[0] += timedelta(minutes=1)
    for execution in (invalid, valid):
        adapter.results[execution.id] = LookupResult("accepted", Decimal("11"), clock[0])

    service.reconcile()
    with factory() as session:
        assert session.get(Execution, invalid.id).state == "unknown"
        assert session.get(VariantGuard, invalid.variant_id).execution_id == invalid.id
        assert session.get(Execution, valid.id).state == "verified"


def test_unknown_lookup_does_not_refresh_unchanged_item_or_batch(safety_context, monkeypatch):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("unchanged-lookup")
    original = executions(factory)[0]
    with factory() as session:
        item_timestamp = session.get(Item, original.item_id).updated_at
    summaries = []
    monkeypatch.setattr(service, "_summarize", lambda batch_id: summaries.append(batch_id))
    clock[0] += timedelta(minutes=1)

    service.reconcile(item_id=original.item_id)
    with factory() as session:
        assert session.get(Item, original.item_id).updated_at == item_timestamp
        assert session.get(Execution, original.id).updated_at == original.updated_at
        assert session.get(VariantGuard, original.variant_id).execution_id == original.id
    assert not summaries


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
    adapter.results[original.id] = LookupResult("accepted", Decimal("11"), observed_at + timedelta(seconds=1))
    service.reconcile(item_id=original.item_id)
    with factory() as session:
        result = session.get(Execution, original.id)
        assert result.state == "verified"
        assert result.evidence["confirmed_at"] == stamp(clock[0])
        assert result.evidence["effective_at"] == stamp(clock[0])


@pytest.mark.parametrize("offset", [-1, 0])
def test_old_or_same_price_observation_cannot_clear_mismatch(safety_context, offset):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    service.run_batch("lookup-observation-order")
    original = executions(factory)[0]
    clock[0] += timedelta(minutes=1)
    latest = clock[0]
    adapter.results[original.id] = LookupResult("accepted", Decimal("12"), latest)
    service.reconcile(item_id=original.item_id)
    clock[0] += timedelta(minutes=1)
    adapter.results[original.id] = LookupResult("accepted", Decimal("11"), latest + timedelta(seconds=offset))
    service.reconcile(item_id=original.item_id)
    with factory() as session:
        current = session.get(Execution, original.id)
        assert current.state == "mismatch"
        assert current.evidence["actual_price"] == "12"
        assert current.evidence["observed_at"] == stamp(latest)
        assert session.get(VariantGuard, original.variant_id).execution_id == original.id


def test_mismatch_and_site_pause_become_visible_in_the_same_commit(safety_context):
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    update_settings(factory, {"paused_sites": ["PH"]})
    service.run_batch("atomic-site-pause")
    original = executions(factory)[0]
    clock[0] += timedelta(minutes=1)
    adapter.results[original.id] = LookupResult("accepted", Decimal("12"), clock[0])

    committed_pauses = []

    def observe_commit(_):
        # 独立读会话模拟同一时刻的操作页或另一轮提交，检查已提交状态。
        with factory() as session:
            if session.get(Execution, original.id).state == "mismatch":
                committed_pauses.append(settings_value(factory)["paused_sites"])

    event.listen(factory.class_, "after_commit", observe_commit)
    try:
        service.reconcile(item_id=original.item_id)
    finally:
        event.remove(factory.class_, "after_commit", observe_commit)

    assert committed_pauses
    assert all(sites == ["MY", "PH"] for sites in committed_pauses)


@pytest.mark.parametrize(("changes", "reason"), [
    ({"paused": True}, "暂停新提交"),
    ({"paused_sites": ["MY"]}, "暂停新提交"),
    ({"pilot_shops": []}, "试点名单"),
    ({"site_enabled": []}, "试点名单"),
    ({"autonomous_enabled": False}, "自动运行尚未启用"),
])
def test_runtime_authorization_revoked_after_claim_prevents_upload(safety_context, changes, reason):
    factory, cfg, adapter, _ = safety_context

    class RevokeAfterClaim(PricingService):
        def _claim(self, item_id, decision):
            execution_id = super()._claim(item_id, decision)
            if execution_id:
                update_settings(factory, changes)
            return execution_id

    batch_id = RevokeAfterClaim(factory, adapter, cfg).run_batch("revoke-after-claim")
    assert adapter.submissions == []
    with factory() as session:
        execution = session.scalar(select(Execution))
        assert execution.state == "failed"
        assert execution.response["status"] == "not_submitted"
        assert session.get(VariantGuard, execution.variant_id).execution_id is None
        items = session.scalars(select(Item).where(Item.batch_id == batch_id)).all()
        assert len(items) == 3
        assert all(item.status == "待处理" and reason in item.reason for item in items)


def test_midnight_while_reading_final_authorization_stops_old_day_upload(safety_context, monkeypatch):
    from app.services import execution as execution_service

    factory, cfg, adapter, clock = safety_context
    clock[0] = clock[0].replace(hour=15, minute=59, second=59)
    read_settings = execution_service.settings_for_submit

    def read_after_midnight(session):
        cfg = read_settings(session)
        clock[0] += timedelta(seconds=2)
        return cfg

    monkeypatch.setattr(execution_service, "settings_for_submit", read_after_midnight)
    batch_id = PricingService(factory, adapter, cfg).run_batch("midnight-at-final-gate")
    assert adapter.submissions == []
    with factory() as session:
        execution = session.scalar(select(Execution))
        assert execution.state == "failed"
        assert execution.response["status"] == "not_submitted"
        assert session.get(VariantGuard, execution.variant_id).execution_id is None
        assert session.get(Item, execution.item_id).status == "待处理"
        jobs = session.scalars(select(Job).where(Job.state == "queued")).all()
        assert [job.dedupe_key for job in jobs] == [f"rollover:{batch_id}:2026-11-06"]


def test_live_price_can_expire_while_waiting_for_final_authorization(safety_context, monkeypatch):
    from app.services import execution as execution_service

    factory, cfg, adapter, clock = safety_context
    cfg.live_submit_enabled = True
    adapter.mode = "live"
    from test_user_pricing_policy import mock_official_zero
    mock_official_zero(monkeypatch,clock)
    adapter.readiness = lambda: {"contract_ready": True, "verified_sites": ["MY"]}
    read_settings = execution_service.settings_for_submit
    first = True

    def delayed_settings(session):
        nonlocal first
        if first:
            clock[0] += timedelta(seconds=301)
            first = False
        return read_settings(session)

    monkeypatch.setattr(execution_service, "settings_for_submit", delayed_settings)
    service = PricingService(factory, adapter, cfg)
    batch_id = service.run_batch("expired-at-final-gate")
    assert [decision.key.item_id for _, decision in adapter.submissions] == ["I1", "I2"]
    with factory() as session:
        item = session.scalar(select(Item).where(Item.key["item_id"].as_string() == "I0"))
        execution = session.scalar(select(Execution).where(Execution.item_id == item.id))
        assert execution.response["status"] == "not_submitted"
        assert "超过5分钟" in item.reason
        assert session.get(VariantGuard, execution.variant_id).execution_id is None
        item_id = item.id
        saved_reason = item.reason
        saved_decision = dict(item.decision)

    assert service.run_batch("expired-at-final-gate") == batch_id
    with factory() as session:
        item = session.get(Item, item_id)
        assert (item.phase, item.reason, item.decision) == ("blocked", saved_reason, saved_decision)
    assert [decision.key.item_id for _, decision in adapter.submissions] == ["I1", "I2"]

    service.run_batch("fresh-reevaluation", item_id=item_id)
    assert [decision.key.item_id for _, decision in adapter.submissions] == ["I1", "I2", "I0"]


def test_live_first_incomplete_snapshot_requeues_daily_job(safety_context, monkeypatch):
    from app.services.store import enqueue
    from app.worker import Worker

    factory, cfg, adapter, clock = safety_context
    adapter.mode = "live"
    from test_user_pricing_policy import mock_official_zero
    mock_official_zero(monkeypatch,clock)
    collect = adapter.collect

    def incomplete_first_read(day):
        snapshot = collect(day)
        if len(adapter.collections) == 1:
            snapshot.lines = []
            snapshot.complete_sources = []
        return snapshot

    monkeypatch.setattr(adapter, "collect", incomplete_first_read)
    update_settings(factory, {"autonomous_enabled": False})
    worker = Worker(factory, PricingService(factory, adapter, cfg))
    job_id = enqueue(factory, "run", {}, "daily:2026-11-05")
    worker.tick(clock[0].timestamp())
    with factory() as session:
        job = session.get(Job, job_id)
        assert job.state == "queued" and job.batch_id is None
        assert session.scalar(select(Batch)) is None
    clock[0] += timedelta(minutes=6)
    worker.tick(clock[0].timestamp())
    with factory() as session:
        job = session.get(Job, job_id)
        assert job.state == "done" and job.batch_id
        assert len(session.scalars(select(Item).where(Item.batch_id == job.batch_id)).all()) == 3
    assert not adapter.submissions


def test_incomplete_retry_preserves_saved_candidates(safety_context, monkeypatch):
    factory, cfg, adapter, _ = safety_context
    collect = adapter.collect

    def interrupted(day):
        snapshot = collect(day)
        if len(adapter.collections) == 2:
            raise SourceUnavailable("refresh failed")
        if len(adapter.collections) == 3:
            snapshot.lines = []
            snapshot.complete_sources = []
        return snapshot

    monkeypatch.setattr(adapter, "collect", interrupted)
    service = PricingService(factory, adapter, cfg)
    with pytest.raises(SourceUnavailable):
        service.run_batch("incomplete-retry")
    with pytest.raises(SourceUnavailable):
        service.run_batch("incomplete-retry")
    assert not adapter.submissions
    with factory() as session:
        assert session.scalar(select(Batch)).status == "failed"
        assert all(item.status == "待处理" for item in session.scalars(select(Item)))
    service.run_batch("incomplete-retry")
    assert len(adapter.submissions) == 3


def test_incomplete_empty_refresh_does_not_report_candidate_disappearance(safety_context, monkeypatch):
    factory, cfg, adapter, _ = safety_context
    collect = adapter.collect

    def incomplete_refresh(day):
        snapshot = collect(day)
        if len(adapter.collections) == 2:
            snapshot.lines = []
            snapshot.complete_sources = []
        return snapshot

    monkeypatch.setattr(adapter, "collect", incomplete_refresh)
    service = PricingService(factory, adapter, cfg)
    with pytest.raises(SourceUnavailable):
        service.run_batch("incomplete-refresh")
    assert not adapter.submissions
    with factory() as session:
        batch = session.scalar(select(Batch))
        assert batch.status == "failed" and "候选来源" in batch.error
        assert all(item.phase == "blocked" and "候选来源" in item.reason
                   for item in session.scalars(select(Item)))



def test_refresh_retains_invalid_candidate_without_submitting_it(safety_context, monkeypatch):
    from app.domain.types import CandidateSourceIssue

    factory, cfg, adapter, _ = safety_context
    collect = adapter.collect

    def refresh_with_bad_row(day):
        snapshot = collect(day)
        if len(adapter.collections) == 2:
            row = snapshot.lines.pop(0)
            snapshot.source_issues.append(CandidateSourceIssue(
                source=row.source, record_number=1, key=row.key, order_id=row.order_id, row_id=row.row_id,
            ))
        return snapshot

    monkeypatch.setattr(adapter, "collect", refresh_with_bad_row)
    PricingService(factory, adapter, cfg).run_batch("isolated-refresh")

    assert {decision.key.item_id for _, decision in adapter.submissions} == {"I1", "I2"}
    with factory() as session:
        item = next(item for item in session.scalars(select(Item)) if item.key["item_id"] == "I0")
        assert item.phase == "blocked" and item.decision["reason_code"] == "source_record_invalid"
        assert item.decision["trace"]["source_issues"][0]["row_id"] == "R0"


def test_failed_refresh_records_waiting_items_and_retries_without_resending(safety_context, monkeypatch):
    factory, cfg, adapter, clock = safety_context
    collect = adapter.collect
    calls = 0

    def interrupted_refresh(day):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise SourceUnavailable("internal source error")
        return collect(day)

    def expire_refresh(_):
        clock[0] += timedelta(minutes=6)

    monkeypatch.setattr(adapter, "collect", interrupted_refresh)
    adapter.on_submit = expire_refresh
    service = PricingService(factory, adapter, cfg)
    with pytest.raises(SourceUnavailable):
        service.run_batch("refresh-retry")
    original = executions(factory)[0]
    with factory() as session:
        batch = session.scalar(select(Batch))
        assert batch.status == "failed"
        assert batch.summary == {"待核验": 1, "待处理": 2}
        batch_id = batch.id
        initial_snapshot = dict(batch.snapshot)
        unsent = session.scalars(select(Item).where(Item.id != original.item_id)).all()
        assert all(item.phase == "blocked" and "刷新失败" in item.reason for item in unsent)
    service.reconcile()
    with factory() as session:
        assert session.get(Batch, batch_id).status == "failed"
    assert service.run_batch("refresh-retry") == batch_id
    assert len(adapter.submissions) == 3
    assert sum(decision.key.target_identity() == original.variant_id
               for _, decision in adapter.submissions) == 1
    with factory() as session:
        batch = session.get(Batch, batch_id)
        assert batch.status == "pending_verification"
        assert batch.error is None
        assert batch.snapshot == initial_snapshot
        assert any(parse_stamp(row.request["trace"]["as_of"]) > parse_stamp(initial_snapshot["as_of"])
                   for row in session.scalars(select(Execution).where(Execution.id != original.id)))


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


def test_unknown_history_before_submit_warns_and_reaches_claim(safety_context, monkeypatch):
    from unittest.mock import Mock
    factory, cfg, adapter, clock = safety_context
    service = PricingService(factory, adapter, cfg)
    adapter.mode='live'
    decision=evaluate(adapter.collect(clock[0].date()),clock[0])[0]
    monkeypatch.setattr(service,'_allowed',lambda *args,**kwargs:(True,''))
    monkeypatch.setattr('app.services.execution.capture_recent_price_changes',lambda *args,**kwargs:{'records':{}})
    claim=Mock(return_value=None)
    monkeypatch.setattr(service,'_claim',claim)
    service._submit('synthetic-item',decision)
    claim.assert_called_once()
    assert decision.ready and 'unknown_history' in decision.trace['warning_codes']
    assert not adapter.submissions
