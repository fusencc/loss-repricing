"""Offline checks for the scheduled department upload and task-result service."""
from contextlib import nullcontext
from pathlib import Path
from shlex import split
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import main, task_results
from app.adapters.base import LookupResult, SubmitResult
from app.config import Settings
from app.db import Base, create_database
from app.models import Execution, Item, Job, VariantGuard
from app.services import erp_department_pricing, erp_workflow, scheduled_departments
from app.services.execution import PricingService
from app.services.records import upload_outcome
from app.services.store import BEIJING, enqueue, utcnow
from test_department_execution import department  # noqa: F401


@pytest.fixture(autouse=True)
def offline_http(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('HTTP is disabled in this offline regression check')

    monkeypatch.setattr(httpx.Client, 'send', unexpected)


def test_ready_department_dispatches_while_another_is_still_calculating(tmp_path, monkeypatch):
    fast, slow = 'Tiktok二十二部', 'Tiktok十二部'
    fast_upload_started = Event()
    uploaded_during_slow_calculation, uploads = [], []
    config = Settings(_env_file=None, runtime_dir=tmp_path, environment='development', adapter_mode='live',
                      database_url=f'sqlite:///{tmp_path / "schedule.db"}', live_history_database_url='',
                      live_submit_enabled=False, ding_enabled=False)
    engine, factory = create_database(config.database_url)
    Base.metadata.create_all(engine)
    day = utcnow().astimezone(BEIJING).date()
    job_id = enqueue(factory, 'run', {'date': day.isoformat()}, 'offline-departments')
    service = SimpleNamespace(config=config, factory=factory, adapter=SimpleNamespace(mode='live'))

    def evaluate(settings, capture, **kwargs):
        if capture['department'] == slow:
            uploaded_during_slow_calculation.append(fast_upload_started.wait(3))
        return {'status': 'completed', 'ready_prices': 1}

    def upload(self, key, prepared):
        uploads.append(prepared)
        if prepared == fast:
            fast_upload_started.set()
        return prepared

    monkeypatch.setattr(erp_workflow, 'ERP_DEPARTMENTS', (fast, slow))
    monkeypatch.setattr(scheduled_departments, 'ERP_DEPARTMENTS', (fast, slow))
    monkeypatch.setattr(erp_workflow, '_session', lambda settings: nullcontext(object()))
    monkeypatch.setattr(erp_workflow, 'refresh_daily_fees', lambda *a, **k: {})
    monkeypatch.setattr(erp_workflow, '_capture_round_fx', lambda settings: None)
    monkeypatch.setattr(erp_workflow, 'capture_erp_bulk', lambda *a, **k: {
        'capture_id': 'offline-group', 'capture_complete': True,
    })
    monkeypatch.setattr(erp_workflow, 'save_capture', lambda folder, capture: capture)
    monkeypatch.setattr(erp_workflow, 'department_captures', lambda parent, names: ({
        name: {'department': name, 'capture_id': name, 'capture_complete': True, 'cleaning_status': 'saved'}
        for name in names
    }, {}))
    monkeypatch.setattr(erp_workflow, '_capture_erp_sources', lambda *a: (None, None))
    monkeypatch.setattr(erp_workflow, '_evaluate_capture', evaluate)
    monkeypatch.setattr(erp_department_pricing, 'load_department_batch', lambda folder, day, scope: scope['department'])
    monkeypatch.setattr(PricingService, 'run_department_batch', upload)

    try:
        with factory() as session:
            job = session.get(Job, job_id)
        scheduled_departments.run_daily_departments(service, job, day)
    finally:
        engine.dispose()
    assert uploaded_during_slow_calculation == [True]
    assert sorted(uploads) == sorted([fast, slow])


def test_daily_worker_resumes_persisted_batch_without_recapture(department, monkeypatch):
    from app.adapters.erp_bulk_capture import ERP_DEPARTMENTS
    from app.worker import Worker

    service, factory, prepared, _, _, submitted = department
    day = prepared.day.isoformat()
    job_id = enqueue(factory, 'run', {'date': day, 'empty_departments': [
        name for name in ERP_DEPARTMENTS if name != prepared.department]}, f'daily:{day}')
    batch_id = service.run_department_batch(job_id, prepared)
    monkeypatch.setattr(scheduled_departments, 'run_workflow', lambda *a, **k: pytest.fail('must not recapture'))

    Worker(factory, service).tick()

    with factory() as session:
        job = session.get(Job, job_id)
        assert job.state == 'done' and job.batch_id == batch_id
        assert job.payload['department_batches'] == {prepared.department: batch_id}
        execution = session.scalar(select(Execution))
        assert execution.state == 'unknown' and submitted == [execution.id]
        assert session.get(VariantGuard, execution.variant_id).execution_id == execution.id


def test_deployed_task_results_entry_can_start_and_only_requests_task_results(tmp_path, monkeypatch):
    unit = Path(__file__).resolve().parents[1] / 'deploy' / 'loss-repricing-task-results.service'
    fields = unit.read_text().splitlines()
    entry = next(line.partition('=')[2] for line in fields if line.startswith('ExecStart='))
    assert split(entry)[1:] == ['-m', 'app.task_results']
    assert 'Environment=LR_LIVE_SUBMIT_ENABLED=false' in fields
    assert 'Environment=LR_DING_ENABLED=false' in fields
    config = Settings(_env_file=None, runtime_dir=tmp_path, environment='development', adapter_mode='live',
                      database_url=f'sqlite:///{tmp_path / "tasks.db"}', live_history_database_url='',
                      admin_password='offline-test', session_secret='x' * 32,
                      live_submit_enabled=False, ding_enabled=False)
    roles, polls = [], []
    configure = main.configure_logging

    def configure_role(settings, role):
        roles.append(role)
        return configure(settings, role)

    class EndOfflinePoll(BaseException):
        pass

    def end_poll(seconds):
        raise EndOfflinePoll

    monkeypatch.setattr(main, 'get_settings', lambda: config)
    monkeypatch.setattr(main, 'configure_logging', configure_role)
    monkeypatch.setattr(main, 'build_adapter', lambda settings: SimpleNamespace(mode='live'))
    monkeypatch.setattr(PricingService, 'reconcile', lambda self, **kwargs: polls.append(kwargs.get('tasks_only')))
    monkeypatch.setattr(task_results, 'time', SimpleNamespace(sleep=end_poll))
    with pytest.raises(EndOfflinePoll):
        task_results.main()
    assert roles == ['worker']
    assert polls == [True]


@pytest.mark.parametrize('submission', ['accepted', 'unknown'])
def test_async_result_only_changes_the_record_on_definite_failure(department, submission, monkeypatch):
    service, factory, prepared, transport, _, submitted = department
    task_queries, price_queries, stages = [], [], []
    lookup = transport.lookup

    def submit(ident, decision):
        submitted.append(ident)
        return SubmitResult(submission, task_id='test-task' if submission == 'accepted' else None)

    def lookup_task(ident, task_id, decision):
        task_queries.append(1)
        return lookup(ident, task_id, decision)

    def lookup_price(*args):
        price_queries.append(1)
        return LookupResult('unknown')

    def snapshot():
        with factory() as session:
            item = session.scalars(select(Item)).one()
            execution = session.scalars(select(Execution)).one()
            locked = session.get(VariantGuard, execution.variant_id).execution_id == execution.id
            display, _ = upload_outcome(item, execution)
            legacy = upload_outcome(SimpleNamespace(status='待核验', reason='offline'), execution)[0]
            state = {'saved': item.status, 'display': display, 'legacy_display': legacy,
                     'execution': execution.state, 'locked': locked, 'task_queries': len(task_queries),
                     'uploads': len(submitted), 'saved_actual_price': 'actual_price' in execution.evidence}
            stages.append(state)
            return state

    monkeypatch.setattr(transport, 'submit', submit)
    monkeypatch.setattr(transport, 'lookup', lookup_task)
    monkeypatch.setattr(service.adapter, 'lookup', lookup_price, raising=False)
    batch = service.run_department_batch('offline-submission', prepared)
    initial = snapshot()
    service.reconcile(tasks_only=True)
    assert service.run_department_batch('offline-retry', prepared) == batch
    unresolved = snapshot()
    transport.outcome = 'rejected'
    service.reconcile(tasks_only=True)
    service.reconcile(tasks_only=True)
    failed = snapshot()
    expected = '已提价' if submission == 'accepted' else '待核验'
    assert not price_queries and not any(stage['saved_actual_price'] for stage in stages)
    assert initial['task_queries'] == 0 and initial['legacy_display'] == expected
    assert initial['saved'] == initial['display'] == unresolved['saved'] == unresolved['display'] == expected
    assert initial['locked'] and unresolved['locked'] and unresolved['uploads'] == 1
    assert failed['saved'] == failed['display'] == '待处理' and failed['execution'] == 'failed'
    assert not failed['locked'] and failed['task_queries'] == 2 and failed['uploads'] == 1
