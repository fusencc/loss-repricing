"""从临时目录中的部门价格归档，经日任务与任务回执，检查公开记录。"""
from datetime import datetime, time
from decimal import Decimal
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters.base import LookupResult, SubmitResult
from app.main import create_app
from app.models import Execution, Job, VariantGuard
from app.services import execution, scheduled_departments, store
from app.worker import Worker
from test_department_execution import department  # noqa: F401
from test_erp_department_pricing import department_artifact  # noqa: F401


def test_daily_archive_to_public_records_with_mixed_receipts(department, department_artifact, monkeypatch):
    import httpx
    import pytest
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_: pytest.fail('unexpected external HTTP'))
    service, factory, _, transport, _, _ = department
    _, report, scope, day = department_artifact
    # 此处要让两条都进入模拟上传；归属冲突另有独立覆盖。
    seller_file = Path(scope['seller_candidates_file'])
    sellers = json.loads(seller_file.read_bytes())['sellers']
    sellers['乙'].extend(sellers.pop('甲'))
    seller_file.write_text(json.dumps({'sellers': sellers}), encoding='utf-8')
    now = datetime.combine(day, time(12), store.BEIJING)
    for module in (store, execution):
        monkeypatch.setattr(module, 'utcnow', lambda: now)
    monkeypatch.setattr(scheduled_departments, 'ERP_DEPARTMENTS', (scope['department'],))
    monkeypatch.setattr(execution.PricingService, '_department_transport', lambda _: transport)
    captures, submissions = [], []

    def collect(config, *, on_department, departments_to_collect):
        assert not config.live_submit_enabled
        captures.append(departments_to_collect)
        on_department(dict(scope, status='completed', ready_prices=2))
        return {'group_capture_id': scope['capture_id']}

    def submit(ident, decision):
        submissions.append((decision.key.item_id, decision.upload_price, ident))
        return SubmitResult('accepted', task_id=ident)

    def lookup(ident, task_id, decision):
        assert ident == task_id
        return LookupResult('rejected' if decision.key.item_id == '0001001' else 'accepted')

    monkeypatch.setattr(scheduled_departments, 'run_workflow', collect)
    monkeypatch.setattr(transport, 'submit', submit)
    monkeypatch.setattr(transport, 'lookup', lookup)
    expected = {row['key']['item_id']: Decimal(row['upload_price']) for row in report['decisions'] if row['ready']}
    assert len(expected) == 2
    job_id = store.enqueue(factory, 'run', {'date': day.isoformat()}, f'daily:{day.isoformat()}')
    worker = Worker(factory, service)
    worker.tick(now.timestamp())
    with factory() as session:
        job = session.get(Job, job_id)
        assert job.state == 'done' and job.payload['department_batches'] == {scope['department']: job.batch_id}
        assert all(session.get(VariantGuard, e.variant_id).execution_id == e.id for e in session.scalars(select(Execution)))
    assert captures == [(scope['department'],)]
    assert len(submissions) == 2 and {sku: price for sku, price, _ in submissions} == expected

    cfg = service.config.model_copy(update={'database_url': str(factory.kw['bind'].url),
        'admin_password': 'offline-fixture', 'session_secret': 's' * 32, 'live_submit_enabled': False})
    monkeypatch.setattr('app.main.build_adapter', lambda _: service.adapter)
    with TestClient(create_app(cfg)) as client:
        def records():
            response = client.get('/api/records', params={'department': 'department:' + scope['department']})
            assert response.status_code == 200
            return {r['key']['item_id']: r['status'] for r in response.json()['items']}

        assert records() == {sku: '已提价' for sku in expected}
        service.reconcile(tasks_only=True)
        service.reconcile(tasks_only=True)
        assert records() == {'0001001': '待处理', '0001002': '已提价'}
    with factory() as session:
        attempts = list(session.scalars(select(Execution)))
        assert sorted(e.state for e in attempts) == ['failed', 'uploaded']
        assert all(session.get(VariantGuard, e.variant_id).execution_id is None for e in attempts)
        assert all('actual_price' not in e.evidence for e in attempts)
    assert len(submissions) == 2
