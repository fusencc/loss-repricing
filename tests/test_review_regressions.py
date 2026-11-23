from datetime import datetime, timedelta, timezone
from collections import Counter
from decimal import Decimal
import importlib.util
from pathlib import Path
import pytest
from sqlalchemy import select
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.adapters.base import LookupResult, SubmitResult
from app.adapters.demo import DemoAdapter
from app.domain.engine import evaluate
from app.domain.types import VariantKey
from app.models import Batch, Item, Execution, VariantGuard, Job
from app.services.execution import PricingService
from app.services.store import enqueue, update_settings, utcnow, BEIJING
from app.worker import Worker
from app.observability import valid_id
from test_execution import context, enable
from test_api import client, login


class RenamedSource(DemoAdapter):
    suffix = ''
    waiting = True
    calls = 0
    lookups = 0

    def collect(self, day):
        snapshot = super().collect(day)
        for row in snapshot.lines + snapshot.online:
            row.key.sku = row.key.item_id + self.suffix
        return snapshot

    def submit(self, *args):
        self.calls += 1
        return super().submit(*args)

    def lookup(self, *args):
        self.lookups += 1
        return LookupResult('unknown') if self.waiting else super().lookup(*args)


def test_sku_rename_cannot_bypass_unknown_or_verified_cooldown(context):
    factory, cfg, _ = context
    source = RenamedSource(cfg.runtime_dir)
    enable(factory)
    service = PricingService(factory, source, cfg)
    service.run_batch('first')
    assert source.calls == 4
    source.suffix = '-renamed'
    service.run_batch('renamed')
    assert source.calls == 4
    source.waiting = False
    service.reconcile()
    source.suffix = '-renamed-again'
    service.run_batch('verified-renamed')
    assert source.calls == 4


def test_ambiguous_aliases_are_blocked_without_combining_loss_samples(context):
    _, _, source = context
    snapshot = source.collect(utcnow().astimezone(BEIJING).date())
    extra = snapshot.lines[0].model_copy(deep=True)
    extra.row_id = 'different-alias'
    extra.key.sku = 'different-local-sku'
    extra.loss_amount = Decimal('-0.01')
    snapshot.lines.append(extra)
    results = [d for d in evaluate(snapshot, utcnow()) if d.key.item_id == extra.key.item_id]
    assert len(results) == 2
    assert all(not d.ready and d.reason_code == 'ambiguous_target_alias' for d in results)


def test_reevaluate_worker_submits_only_selected_target_and_unknown_only_queries(context):
    factory, cfg, _ = context
    source = RenamedSource(cfg.runtime_dir)
    service = PricingService(factory, source, cfg)
    batch = service.run_batch('held')
    with factory() as session:
        selected = session.scalar(select(Item).where(Item.batch_id == batch, Item.key['item_id'].as_string() == 'DEMO-I000'))
        item_id = selected.id
    enable(factory)
    source.suffix = '-renamed'
    job_id = enqueue(factory, 'run', {'item_id': item_id})
    worker = Worker(factory, service)
    worker.tick()
    assert source.calls == 1
    with factory() as session:
        job = session.get(Job, job_id)
        rows = session.scalars(select(Item).where(Item.batch_id == job.batch_id)).all()
        assert len(rows) == 1
        assert rows[0].key['item_id'] == 'DEMO-I000'
    previous_lookups = source.lookups
    enqueue(factory, 'run', {'item_id': item_id})
    worker.tick()
    assert source.calls == 1
    assert source.lookups > previous_lookups


@pytest.mark.parametrize("scheduled", [False, True])
def test_midnight_stops_cached_submissions_and_queues_fresh_day(context, monkeypatch, scheduled):
    factory, cfg, source = context
    today = utcnow().astimezone(BEIJING).date()
    clock = [datetime.combine(today, datetime.min.time(), BEIJING).replace(hour=23, minute=59, second=59).astimezone(timezone.utc)]
    template = source.collect(today)
    calls, collected = [], []
    def collect(day):
        collected.append(day)
        snapshot = template.model_copy(deep=True)
        snapshot.as_of = clock[0]
        snapshot.business_date = snapshot.fx_date = day
        for online in snapshot.online:
            online.observed_at = clock[0]
            online.profit_check.inventory_observed_at = clock[0]
        return snapshot
    def submit(execution_id, decision):
        calls.append((decision.trace['business_date'], clock[0].astimezone(BEIJING).date().isoformat()))
        clock[0] += timedelta(seconds=2)
        return SubmitResult('unknown')
    monkeypatch.setattr('app.services.execution.utcnow', lambda: clock[0])
    monkeypatch.setattr('app.services.store.utcnow', lambda: clock[0])
    monkeypatch.setattr(source, 'collect', collect)
    monkeypatch.setattr(source, 'submit', submit)
    enable(factory)
    service = PricingService(factory, source, cfg)
    worker = Worker(factory, service)
    if scheduled:
        from app.services.store import enqueue

        job_id = enqueue(factory, "run", {"date": today.isoformat()}, f"daily:{today.isoformat()}")
        worker.tick(clock[0].timestamp())
        with factory() as session:
            first = session.get(Job, job_id).batch_id
    else:
        first = service.run_batch('midnight')
    assert len(calls) == 1
    with factory() as session:
        queued = session.scalars(select(Job).where(Job.state == 'queued')).all()
        assert len(queued) == 1
        if scheduled:
            assert queued[0].dedupe_key == f'daily:{today + timedelta(days=1)}'
        assert session.get(Batch, first).business_date == today.isoformat()
    worker.tick(clock[0].timestamp())
    assert len(calls) == 4
    assert all(decision_day == submit_day for decision_day, submit_day in calls)
    assert today + timedelta(days=1) in collected


def test_current_items_and_counts_omit_resolved_history_but_preserve_batches(client):
    login(client)
    state = client.app.state
    first = state.service.run_batch('held')
    enable(state.factory)
    state.service.run_batch('enabled')
    current = client.get('/api/items').json()
    verified = {row['key']['item_id'] for row in current if row['status'] == '已核验'}
    assert len(verified) == 4
    pending = client.get('/api/items?status=待处理').json()
    assert not any(row['key']['item_id'] in verified for row in pending)
    assert len(current) == 8
    assert client.get('/api/overview').json()['counts'] == {**dict.fromkeys(['本轮不调', '待处理', '待核验', '已核验'], 0), **Counter(row['status'] for row in current)}
    history = client.get('/api/items', params={'batch_id': first, 'status': '待处理'}).json()
    assert any(row['key']['item_id'] in verified for row in history)
    assert len(client.get(f'/api/batches/{first}').json()['items']) == 8


def test_current_items_keep_pending_execution_even_after_newer_blocked_record(client):
    login(client)
    state = client.app.state
    source = RenamedSource(state.config.runtime_dir)
    service = PricingService(state.factory, source, state.config)
    enable(state.factory)
    first = service.run_batch('unknown')
    source.suffix = '-renamed'
    second = service.run_batch('blocked')
    rows = client.get('/api/items?status=待核验').json()
    assert len(rows) == 4
    assert all(row['batch_id'] == first and row['execution']['state'] in {'accepted', 'unknown'} for row in rows)
    current = client.get('/api/items').json()
    assert len(current) == 12  # 最新8条，加上仍未核验的旧4条。
    assert all('target_id' in row for row in current)
    with state.factory() as session:
        assert session.get(Batch, second)


def migrate_legacy(factory):
    # 执行已提交初始版本之后的正式Alembic数据迁移（数据库本身由测试隔离创建）。
    engine = factory.kw['bind']
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            for path in sorted(Path('backend/migrations/versions').glob('*.py')):
                spec = importlib.util.spec_from_file_location('test_data_migration', path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if module.down_revision == '3e2856f6b847':
                    module.upgrade()


def legacy_guards(factory):
    # 模拟上一版本持久化数据：执行和锁都使用带SKU的旧hash。
    with factory.begin() as session:
        sessions = session.scalars(select(Execution)).all()
        session.query(VariantGuard).delete()
        for execution in sessions:
            key = VariantKey.model_validate(execution.request['key'])
            execution.variant_id = key.identity()
            session.add(VariantGuard(variant_id=key.identity(),
                execution_id=execution.id if execution.state != 'verified' else None,
                last_effective_at=execution.evidence.get('effective_at')))


def test_migration_preserves_legacy_verified_cooldown_and_json_history(context):
    factory, cfg, _ = context
    source = RenamedSource(cfg.runtime_dir)
    source.waiting = False
    enable(factory)
    service = PricingService(factory, source, cfg)
    service.run_batch('old-success')
    legacy_guards(factory)
    with factory() as session:
        before = {e.id: e.request for e in session.scalars(select(Execution))}
    migrate_legacy(factory)
    source.suffix = '-after-migration'
    service.run_batch('renamed-after-migration')
    assert source.calls == 4
    with factory() as session:
        assert {e.id: e.request for e in session.scalars(select(Execution))} == before


def test_migration_keeps_all_legacy_pending_until_each_is_reconciled(context):
    from uuid import uuid4
    factory, cfg, _ = context
    source = RenamedSource(cfg.runtime_dir)
    service = PricingService(factory, source, cfg)
    enable(factory)
    service.run_batch('legacy-unknown')
    legacy_guards(factory)
    with factory.begin() as session:
        original = session.scalar(select(Execution).join(Item).where(Item.key['item_id'].as_string() == 'DEMO-I000'))
        item = session.get(Item, original.item_id)
        alias = VariantKey.model_validate(item.key)
        alias.sku += '-second-old-alias'
        item_data = {c.name: getattr(item, c.name) for c in Item.__table__.columns}
        item_data.update(id=str(uuid4()), variant_id=alias.identity(), key=alias.model_dump(mode='json'))
        extra_item = Item(**item_data)
        session.add(extra_item)
        session.flush()
        execution_data = {c.name: getattr(original, c.name) for c in Execution.__table__.columns}
        execution_data.update(id=str(uuid4()), item_id=extra_item.id, variant_id=alias.identity(),
                              request={**original.request, 'key': alias.model_dump(mode='json')})
        extra = Execution(**execution_data)
        session.add(extra)
        session.add(VariantGuard(variant_id=alias.identity(), execution_id=extra.id))
        first_id, second_id = original.id, extra.id
    migrate_legacy(factory)
    source.suffix = '-new-alias'
    service.run_batch('after-migration')
    assert source.calls == 4
    rejected = {first_id}
    source.lookup = lambda eid, *args: LookupResult('rejected') if eid in rejected else LookupResult('unknown')
    service.reconcile()
    service.run_batch('one-resolved')
    assert source.calls == 4
    rejected.add(second_id)
    service.reconcile()
    service.run_batch('both-resolved')
    assert source.calls == 5


@pytest.mark.parametrize('cross_at', ['refresh', 'evaluation'])
def test_rollover_during_refresh_preserves_selected_target(context, monkeypatch, cross_at):
    factory, cfg, source = context
    service = PricingService(factory, source, cfg)
    initial = service.run_batch('held-for-single-midnight')
    with factory() as session:
        selected = session.scalar(select(Item).where(Item.batch_id == initial, Item.key['item_id'].as_string() == 'DEMO-I000'))
        selected_id = selected.id
    today = utcnow().astimezone(BEIJING).date()
    clock = [datetime.combine(today, datetime.min.time(), BEIJING).replace(hour=23, minute=59, second=59).astimezone(timezone.utc)]
    template = source.collect(today)
    collections, calls = [], []
    def collect(day):
        collections.append(day)
        snapshot = template.model_copy(deep=True)
        snapshot.as_of = clock[0]
        snapshot.business_date = snapshot.fx_date = day
        for state in snapshot.online:
            state.observed_at = clock[0]
            state.profit_check.inventory_observed_at = clock[0]
        if len(collections) == 2 and cross_at == 'refresh':
            clock[0] += timedelta(seconds=2)
        return snapshot
    evaluations = []
    def evaluate_at_boundary(snapshot, now):
        evaluations.append(now)
        if len(evaluations) == 2 and cross_at == 'evaluation':
            clock[0] += timedelta(seconds=2)
            now = clock[0]
        return evaluate(snapshot, now)
    monkeypatch.setattr('app.services.execution.evaluate', evaluate_at_boundary)
    monkeypatch.setattr('app.services.execution.utcnow', lambda: clock[0])
    monkeypatch.setattr('app.services.store.utcnow', lambda: clock[0])
    monkeypatch.setattr(source, 'collect', collect)
    monkeypatch.setattr(source, 'submit', lambda eid, d: calls.append(d.key.item_id) or SubmitResult('unknown'))
    enable(factory)
    service.run_batch('single-midnight', item_id=selected_id)
    assert calls == []
    with factory() as session:
        job = session.scalar(select(Job).where(Job.state == 'queued'))
        assert job is not None
        # 允许新增技术关联元数据，但业务负载仍只能指向原选中记录，不扩大执行范围。
        assert {key: value for key, value in job.payload.items() if key != '_log_context'} == {'item_id': selected_id}
        assert set(job.payload['_log_context']) == {'operation_id'}
        assert valid_id(job.payload['_log_context']['operation_id'])
    Worker(factory, service).tick(clock[0].timestamp())
    assert calls == ['DEMO-I000']


def test_migration_uses_latest_guard_time_and_keeps_it_after_pending_rejection(context):
    from uuid import uuid4
    factory, cfg, _ = context
    source = RenamedSource(cfg.runtime_dir)
    service = PricingService(factory, source, cfg)
    enable(factory)
    service.run_batch('legacy-latest-time')
    legacy_guards(factory)
    now = utcnow()
    with factory.begin() as session:
        original = session.scalar(select(Execution).join(Item).where(Item.key['item_id'].as_string() == 'DEMO-I000'))
        original_guard = session.get(VariantGuard, original.variant_id)
        original_guard.last_effective_at = (now - timedelta(hours=80)).isoformat()
        item = session.get(Item, original.item_id)
        alias = VariantKey.model_validate(item.key)
        alias.sku += '-verified-old-alias'
        data = {c.name: getattr(item, c.name) for c in Item.__table__.columns}
        data.update(id=str(uuid4()), variant_id=alias.identity(), key=alias.model_dump(mode='json'),
                    status='已核验', phase='verified')
        session.add(Item(**data))
        session.add(VariantGuard(variant_id=alias.identity(), last_effective_at=(now - timedelta(hours=1)).isoformat()))
        pending_id = original.id
    migrate_legacy(factory)
    source.lookup = lambda eid, *args: LookupResult('rejected') if eid == pending_id else LookupResult('unknown')
    service.reconcile()
    source.suffix = '-renamed-after-rejection'
    service.run_batch('cooldown-after-legacy-rejection')
    assert source.calls == 4


def test_migration_unmappable_protected_guard_fails_without_dropping_history(context):
    factory, _, _ = context
    with factory.begin() as session:
        session.add(VariantGuard(variant_id='unmappable-old-key', execution_id='unknown-old-request'))
    with pytest.raises(RuntimeError, match='无法还原历史锁目标'):
        migrate_legacy(factory)
    with factory() as session:
        assert session.get(VariantGuard, 'unmappable-old-key').execution_id == 'unknown-old-request'


def test_day_changes_after_claim_never_calls_upload_and_releases_only_unsent_intent(context, monkeypatch):
    factory, cfg, source = context
    service = PricingService(factory, source, cfg)
    today = utcnow().astimezone(BEIJING).date()
    clock = [datetime.combine(today, datetime.min.time(), BEIJING).replace(hour=23, minute=59, second=59).astimezone(timezone.utc)]
    monkeypatch.setattr('app.services.execution.utcnow', lambda: clock[0])
    monkeypatch.setattr('app.services.store.utcnow', lambda: clock[0])
    original_claim = service._claim
    def claim(*args):
        execution_id = original_claim(*args)
        clock[0] += timedelta(seconds=2)
        return execution_id
    monkeypatch.setattr(service, '_claim', claim)
    calls = []
    monkeypatch.setattr(source, 'submit', lambda *args: calls.append(args) or SubmitResult('unknown'))
    enable(factory)
    service.run_batch('claim-at-midnight')
    assert calls == []
    with factory() as session:
        executions = session.scalars(select(Execution)).all()
        assert len(executions) == 1
        assert executions[0].response['status'] == 'not_submitted'
        assert session.get(VariantGuard, executions[0].variant_id).execution_id is None
        assert session.query(Job).filter(Job.state == 'queued').count() == 1


def test_direct_claim_serializes_aliases_even_with_precomputed_decisions(context):
    from uuid import uuid4
    factory, cfg, source = context
    service = PricingService(factory, source, cfg)
    batch = service.run_batch('precomputed')
    decisions = evaluate(source.collect(utcnow().astimezone(BEIJING).date()), utcnow())
    first = next(d for d in decisions if d.key.item_id == 'DEMO-I000')
    second = first.model_copy(deep=True)
    second.key.sku += '-alias'
    with factory.begin() as session:
        item = session.scalar(select(Item).where(Item.batch_id == batch, Item.key['item_id'].as_string() == 'DEMO-I000'))
        first_id = item.id
        data = {c.name: getattr(item, c.name) for c in Item.__table__.columns}
        second_id = str(uuid4())
        data.update(id=second_id, variant_id=second.key.identity(), key=second.key.model_dump(mode='json'))
        session.add(Item(**data))
    assert service._claim(first_id, first)
    assert service._claim(second_id, second) is None
    with factory() as session:
        assert session.query(Execution).count() == 1


@pytest.mark.parametrize('outcome', ['verified', 'failed'])
def test_execution_dependent_proposal_follows_terminal_outcome_without_rewriting_history(client, outcome):
    login(client)
    state = client.app.state
    source = RenamedSource(state.config.runtime_dir)
    service = PricingService(state.factory, source, state.config)
    enable(state.factory)
    submitted = service.run_batch('pending-original')
    blocked = service.run_batch('pending-dependent')
    historical_before = client.get(f'/api/batches/{blocked}').json()
    if outcome == 'verified':
        source.waiting = False
    else:
        source.lookup = lambda *args: LookupResult('rejected', message='确认逐行拒绝')
    service.reconcile()
    current = client.get('/api/items').json()
    affected = [row for row in current if row['key']['item_id'] in {'DEMO-I000', 'DEMO-I001', 'DEMO-I004', 'DEMO-I006'}]
    assert len(affected) == 4
    assert all(row['batch_id'] == submitted and row['execution']['state'] == outcome for row in affected)
    assert all(row['phase'] == outcome for row in affected)
    pending = client.get('/api/items?status=待处理').json()
    if outcome == 'verified':
        assert not any(row['id'] in {r['id'] for r in affected} for row in pending)
        assert client.get('/api/overview').json()['counts']['已核验'] == 4
    else:
        assert all(row in pending for row in affected)
        assert all(row['reason'] == '确认逐行拒绝' for row in affected)
    assert client.get(f'/api/batches/{blocked}').json() == historical_before
    assert source.calls == 4


def test_terminal_dependency_does_not_hide_newer_independent_mapping_exception(client):
    login(client)
    state = client.app.state
    source = RenamedSource(state.config.runtime_dir)
    service = PricingService(state.factory, source, state.config)
    enable(state.factory)
    service.run_batch('pending-with-new-business-exception')
    original_collect = source.collect
    def ambiguous_collect(day):
        snapshot = original_collect(day)
        alias = snapshot.lines[0].model_copy(deep=True)
        alias.key.sku += '-ambiguous'
        alias.row_id += '-ambiguous'
        snapshot.lines.append(alias)
        return snapshot
    source.collect = ambiguous_collect
    blocked = service.run_batch('new-business-exception')
    source.waiting = False
    service.reconcile()
    affected = [row for row in client.get('/api/items').json() if row['key']['item_id'] == 'DEMO-I000']
    assert len(affected) == 1
    assert affected[0]['batch_id'] == blocked
    assert affected[0]['decision']['reason_code'] == 'ambiguous_target_alias'
    assert affected[0]['status'] == '待处理'
    assert source.calls == 4


def test_current_projection_keeps_multiple_pending_dependencies_until_all_resolve(client):
    from uuid import uuid4
    login(client)
    state = client.app.state
    source = RenamedSource(state.config.runtime_dir)
    service = PricingService(state.factory, source, state.config)
    enable(state.factory)
    service.run_batch('two-old-pending')
    with state.factory.begin() as session:
        original = session.scalar(select(Execution).join(Item).where(Item.key['item_id'].as_string() == 'DEMO-I000'))
        item = session.get(Item, original.item_id)
        key = VariantKey.model_validate(item.key)
        key.sku += '-old-alias'
        data = {c.name: getattr(item, c.name) for c in Item.__table__.columns}
        alias_item_id = str(uuid4())
        data.update(id=alias_item_id, variant_id=key.identity(), key=key.model_dump(mode='json'))
        session.add(Item(**data))
        session.flush()
        data = {c.name: getattr(original, c.name) for c in Execution.__table__.columns}
        data.update(id=str(uuid4()), item_id=alias_item_id, request={**original.request, 'key': key.model_dump(mode='json')})
        session.add(Execution(**data))
        original_id = original.id
    service.run_batch('blocked-behind-two-pending')
    rows = [row for row in client.get('/api/items').json() if row['key']['item_id'] == 'DEMO-I000']
    assert len(rows) == 3
    assert len([row for row in rows if row['execution']]) == 2
    source.lookup = lambda eid, *args: LookupResult('rejected') if eid == original_id else LookupResult('unknown')
    service.reconcile()
    rows = [row for row in client.get('/api/items').json() if row['key']['item_id'] == 'DEMO-I000']
    assert len(rows) == 2
    assert any(row['id'] == alias_item_id and row['status'] == '待核验' for row in rows)
    assert any(row['decision']['reason_code'] == 'pending_submission' for row in rows)
    assert source.calls == 4


def test_old_terminal_result_does_not_replace_new_external_pending_source(client):
    login(client)
    state = client.app.state
    source = RenamedSource(state.config.runtime_dir)
    source.waiting = False
    service = PricingService(state.factory, source, state.config)
    enable(state.factory)
    service.run_batch('already-verified')
    original_collect = source.collect
    def external_pending(day):
        snapshot = original_collect(day)
        for row in snapshot.online:
            row.pending_submission = True
        return snapshot
    source.collect = external_pending
    current_batch = service.run_batch('new-external-pending')
    rows = client.get('/api/items').json()
    assert len(rows) == 8
    assert all(row['batch_id'] == current_batch and row['decision']['reason_code'] == 'pending_submission' for row in rows)
    assert source.calls == 4
