from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.adapters.base import LookupResult, SubmitResult
from app.adapters.erp_price_upload import ERP_UPLOAD_TRACE
from app.config import Settings
from app.db import Base, create_database
from app.domain.engine import evaluate
from app.models import Batch, Execution, Item, Protection, VariantGuard
from app.services.erp_department_pricing import DepartmentPrice, DepartmentPriceBatch
from app.services.execution import PricingService
from app.services.records import record_department, record_seller
from app.services.store import BEIJING, update_settings, utcnow
from fake_adapter import DemoAdapter


@pytest.fixture
def department(tmp_path, monkeypatch):
    day = utcnow().astimezone(BEIJING).date()
    snapshot = DemoAdapter(tmp_path).collect(day)
    candidate = next(d for d in evaluate(snapshot, snapshot.as_of) if d.ready)
    price = DepartmentPrice(candidate.upload_price, candidate.target_actual, '测试店铺', 'direct', '测试销售员')
    prepared = DepartmentPriceBatch(day, 'Tiktok一部', snapshot, [candidate], {candidate.key.identity(): price})
    engine, factory = create_database(f"sqlite:///{tmp_path / 'department.sqlite'}")
    Base.metadata.create_all(engine)
    update_settings(factory, {'autonomous_enabled': True})
    settings = Settings(_env_file=None, runtime_dir=tmp_path, adapter_mode='live', live_submit_enabled=True,
                        coordination_required=True)
    service = PricingService(factory, SimpleNamespace(mode='live', synthetic=False), settings)
    checked, submitted = [], []
    class Transport:
        outcome = 'unknown'
        client = auth = None
        def prepare(self, ident, batch, day, shop, decision):
            checked.append(ident)
            return {'shop': shop, 'execution_id': ident}
        def submit(self, ident, decision):
            submitted.append(ident)
            assert decision.trace[ERP_UPLOAD_TRACE]['execution_id'] == ident
            return SubmitResult('unknown')
        def lookup(self, ident, task_id, decision):
            assert ident in submitted
            return LookupResult(self.outcome, actual_price=decision.target_actual, observed_at=utcnow())
    transport = Transport()
    monkeypatch.setattr(service, '_department_transport', lambda: transport)
    def history(config, keys, *, now, client, auth):
        return {'records': {k.item_id: {'status': 'ok', 'count': 0} for k in keys},
                'window_start': now - timedelta(hours=72), 'window_end': now, 'source_id': 'test'}
    monkeypatch.setattr('app.services.execution.capture_recent_price_changes', history)
    yield service, factory, prepared, transport, checked, submitted
    engine.dispose()


def test_department_retry_preserves_original_intent_and_reconciles_task_result(department):
    service, factory, prepared, transport, checked, submitted = department
    batch_id = service.run_department_batch('initial', prepared)
    assert service.run_department_batch('new-job-id', prepared) == batch_id
    service.reconcile()
    assert len(checked) == len(submitted) == 1
    with factory() as session:
        batch = session.get(Batch, batch_id)
        item = session.scalars(select(Item)).one()
        execution = session.scalars(select(Execution)).one()
        assert execution.state == 'unknown'
        assert session.get(VariantGuard, execution.variant_id).execution_id == execution.id
        assert record_department(item, batch) == prepared.department
        assert record_seller(item, batch, {}, {})['name'] == '测试销售员'
    transport.outcome = 'accepted'
    service.reconcile()
    with factory() as session:
        result = session.get(Execution, submitted[0])
        assert result.state == 'uploaded'
        assert 'actual_price' not in result.evidence
        assert session.get(VariantGuard, result.variant_id).execution_id is None
        assert session.get(Batch, batch_id).status == 'completed'
    service.run_department_batch('third-job', prepared)
    prepared.department = 'Tiktok五部'
    other = service.run_department_batch('other-department', prepared)
    with factory() as session:
        duplicate = session.scalars(select(Item).where(Item.batch_id == other)).one()
        assert duplicate.status == '不调价' and '72小时' in duplicate.reason
    assert len(submitted) == 1


def test_department_submits_one_salesperson_at_a_time(department, monkeypatch):
    from dataclasses import replace

    service, _, prepared, _, _, _ = department
    original, = prepared.decisions
    price = prepared.prices[original.key.identity()]
    prepared.decisions = []
    for n, seller in enumerate(('B', 'A', 'B', None, 'A')):
        key = original.key.model_copy(update={'item_id': f'item-{n}'})
        prepared.decisions.append(original.model_copy(update={'key': key}))
        prepared.prices[key.identity()] = replace(price, salesperson=seller)

    order = []
    def submit(item_id, decision, day, *, department, batch_id):
        order.append(department['salesperson'])
    monkeypatch.setattr(service, '_submit', submit)
    service.run_department_batch('seller-groups', prepared)
    assert sorted(s or '' for s in order) == ['', 'A', 'A', 'B', 'B']
    assert order.index('A') + 1 == max(i for i, s in enumerate(order) if s == 'A')
    assert order.index('B') + 1 == max(i for i, s in enumerate(order) if s == 'B')


def test_department_honors_manual_protection_and_scope_pause(department):
    service, factory, prepared, _, _, submitted = department
    decision = prepared.decisions[0]
    with factory.begin() as session:
        session.add(Protection(id='protected', created_at=utcnow().isoformat(),
                               shop_id=decision.key.shop_id, site=decision.key.site, active=True,
                               main_sku=decision.trace['online_rows'][0]['main_sku'], reason='测试保护'))
    service.run_department_batch('protected', prepared)
    with factory() as session:
        assert session.scalars(select(Item)).one().status == '不调价'
    prepared.department = 'Tiktok五部'
    update_settings(factory, {'paused_sites': [decision.key.site]})
    batch_id = service.run_department_batch('paused', prepared)
    with factory() as session:
        item = session.scalars(select(Item).where(Item.batch_id == batch_id)).one()
        assert '暂停' in item.reason
        assert session.scalar(select(Execution.id)) is None
    assert submitted == []


def test_department_crash_after_saving_intent_recovers_without_resubmitting(department):
    service, factory, prepared, transport, _, submitted = department
    class Interrupted(BaseException):
        pass
    def interrupted(ident, decision):
        from app.models import Notice
        with factory() as session:
            notice = session.scalar(select(Notice))
            assert notice.summary['erp_salesperson'] == '测试销售员' and notice.delivery_state == 'pending'
        submitted.append(ident)
        raise Interrupted
    transport.submit = interrupted
    with pytest.raises(Interrupted):
        service.run_department_batch('interrupted', prepared)
    with factory() as session:
        execution = session.scalars(select(Execution)).one()
        assert execution.state == 'submitting'
        assert execution.request['trace'][ERP_UPLOAD_TRACE]['execution_id'] == execution.id
        assert session.get(VariantGuard, execution.variant_id).execution_id == execution.id
    service.run_department_batch('retry', prepared)
    transport.outcome = 'accepted'
    service.reconcile()
    with factory() as session:
        execution = session.scalars(select(Execution)).one()
        assert execution.state == 'uploaded' and submitted == [execution.id]


def test_acceptance_is_priced_and_only_task_failure_becomes_pending(department):
    service, factory, prepared, transport, _, submitted = department
    def accepted(ident, decision):
        submitted.append(ident)
        return SubmitResult('accepted', task_id='task')
    transport.submit = accepted
    service.run_department_batch('accepted', prepared)
    with factory() as session:
        assert session.scalars(select(Item)).one().status == '已提价'
    service.reconcile(tasks_only=True)
    with factory() as session:
        assert session.scalars(select(Item)).one().status == '已提价'
    transport.outcome = 'rejected'
    service.reconcile(tasks_only=True)
    with factory() as session:
        assert session.scalars(select(Item)).one().status == '待处理'
        assert session.scalars(select(Execution)).one().state == 'failed'


def test_concurrent_result_query_skips_without_duplicate_request(department):
    import fcntl
    service, _, prepared, transport, _, submitted = department
    service.run_department_batch('locked', prepared)
    with (service.config.runtime_dir / 'task-results.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        transport.lookup = lambda *_: pytest.fail('duplicate query')
        service.reconcile(tasks_only=True)
    assert len(submitted) == 1
