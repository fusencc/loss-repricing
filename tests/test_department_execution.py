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


def test_department_retry_preserves_original_intent_and_reconciles_actual_price(department):
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
        assert result.state == 'verified'
        assert result.evidence['actual_price'] == str(prepared.decisions[0].target_actual)
        assert session.get(VariantGuard, result.variant_id).execution_id is None
        assert session.get(Batch, batch_id).status == 'completed'
    service.run_department_batch('third-job', prepared)
    prepared.department = 'Tiktok五部'
    other = service.run_department_batch('other-department', prepared)
    with factory() as session:
        duplicate = session.scalars(select(Item).where(Item.batch_id == other)).one()
        assert duplicate.status == '不调价' and '72小时' in duplicate.reason
    assert len(submitted) == 1


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
        assert execution.state == 'verified' and submitted == [execution.id]
