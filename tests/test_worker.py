from datetime import datetime, timezone
from app.config import Settings
from app.db import Base, create_database
from app.models import Job
from app.adapters.base import SourceUnavailable
from app.services.store import enqueue, update_settings
from app.worker import Worker


class FakeService:
    calls = 0
    ready = True
    def run_batch(self, run_key):
        self.calls += 1
        if not self.ready:
            raise SourceUnavailable("当日汇率尚未更新")
        return "batch-result"
    def reconcile(self):
        pass


def setup(tmp_path):
    engine, factory = create_database(f"sqlite:///{tmp_path/'worker.db'}")
    Base.metadata.create_all(engine)
    return engine, factory, FakeService()


def test_job_completes_and_schedule_creates_only_one_daily_job(tmp_path):
    engine, factory, service = setup(tmp_path)
    update_settings(factory, {"schedule_enabled": True})
    worker = Worker(factory, service)
    now = datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc).timestamp()
    worker.tick(now)
    worker.tick(now + 5)
    assert service.calls == 1
    with factory() as session:
        assert session.query(Job).count() == 1
        assert session.query(Job).first().state == "done"
    engine.dispose()


def test_schedule_starts_at_beijing_nine(tmp_path):
    engine, factory, service = setup(tmp_path)
    update_settings(factory, {"schedule_enabled": True})
    worker = Worker(factory, service)
    before = datetime(2026, 11, 5, 0, 59, 59, tzinfo=timezone.utc).timestamp()
    worker.tick(before)
    assert service.calls == 0
    with factory() as session:
        assert session.query(Job).count() == 0
    worker.tick(before + 1)
    worker.tick(before + 6)
    assert service.calls == 1
    with factory() as session:
        job = session.query(Job).one()
        assert job.dedupe_key == "daily:2026-11-05"
        assert job.state == "done"
    engine.dispose()


def test_schedule_disabled_at_beijing_nine(tmp_path):
    engine, factory, service = setup(tmp_path)
    update_settings(factory, {"schedule_enabled": False})
    worker = Worker(factory, service)
    worker.tick(datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc).timestamp())
    assert service.calls == 0
    with factory() as session:
        assert session.query(Job).count() == 0
    engine.dispose()


def test_not_ready_retries_after_five_minutes_without_marking_success(tmp_path):
    engine, factory, service = setup(tmp_path)
    service.ready = False
    job_id = enqueue(factory, "run", {})
    worker = Worker(factory, service)
    worker.tick(100)
    worker.tick(101)
    assert service.calls == 1
    with factory() as session:
        assert session.get(Job, job_id).state == "queued"
    service.ready = True
    worker.tick(401)
    assert service.calls == 2
    with factory() as session:
        assert session.get(Job, job_id).state == "done"
    engine.dispose()


def test_second_worker_cannot_consume_while_leader_alive(tmp_path):
    engine, factory, service = setup(tmp_path)
    worker_a, worker_b = Worker(factory, service), Worker(factory, service)
    worker_a.tick(100)
    enqueue(factory, "run", {})
    worker_b.tick(101)
    assert service.calls == 0
    worker_b.tick(300)
    assert service.calls == 1
    engine.dispose()


def test_slow_source_failure_waits_five_minutes_from_failure(tmp_path, monkeypatch):
    engine, factory, service = setup(tmp_path)
    elapsed = [0]
    reconciles = []
    monkeypatch.setattr("app.worker.time.monotonic", lambda: elapsed[0])
    monkeypatch.setattr(service, "reconcile", lambda: reconciles.append(True))
    original = service.run_batch
    def slow_source(run_key):
        if not service.ready:
            elapsed[0] += 360
        return original(run_key)
    service.run_batch = slow_source
    service.ready = False
    job_id = enqueue(factory, "run", {})
    worker = Worker(factory, service)
    worker.tick(100)
    with factory() as session:
        job = session.get(Job, job_id)
        assert job.state == "queued" and job.lease_until == 760
    service.ready = True
    worker.tick(461)
    worker.tick(759)
    assert service.calls == 1 and len(reconciles) >= 2
    worker.tick(760)
    assert service.calls == 2
    with factory() as session:
        assert session.get(Job, job_id).state == "done"
    engine.dispose()


def test_expired_daily_retry_does_not_process_the_new_business_day(tmp_path):
    engine, factory, service = setup(tmp_path)
    yesterday = enqueue(factory, "run", {"date": "2026-11-05"}, "daily:2026-11-05")
    update_settings(factory, {"schedule_enabled": True})
    worker = Worker(factory, service)
    morning = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc).timestamp()
    worker.tick(morning)
    worker.tick(morning + 5)
    assert service.calls == 1
    with factory() as session:
        assert session.get(Job, yesterday).state == "cancelled"
        assert session.query(Job).filter_by(dedupe_key="daily:2026-11-06").one().state == "done"
    engine.dispose()


def test_notice_dispatch_failure_does_not_skip_reconciliation_or_daily_job(tmp_path, monkeypatch):
    engine, factory, service = setup(tmp_path)
    service.config = Settings(_env_file=None, runtime_dir=tmp_path)
    reconciles, logged = [], []
    def unavailable(*_):
        raise RuntimeError("notification dispatch failed")
    monkeypatch.setattr("app.services.notifications.dispatch_notices", unavailable)
    monkeypatch.setattr(service, "reconcile", lambda: reconciles.append(True))
    monkeypatch.setattr("app.worker.event", lambda name, **kwargs: logged.append(name))
    update_settings(factory, {"schedule_enabled": True})
    Worker(factory, service).tick(datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc).timestamp())
    assert reconciles == [True] and service.calls == 1
    assert "worker.notice_dispatch_failed" in logged
    with factory() as session:
        job = session.query(Job).filter_by(dedupe_key="daily:2026-11-05").one()
        assert job.state == "done" and job.batch_id == "batch-result"
    engine.dispose()


def test_former_roster_day_does_not_override_disabled_schedule(tmp_path):
    engine, factory, service = setup(tmp_path)
    service.config = Settings(_env_file=None, runtime_dir=tmp_path, coordination_required=False)
    worker = Worker(factory, service)
    for hour in (23, 1):
        day = 25 if hour == 23 else 26
        worker.tick(datetime(2026, 11, day, hour, tzinfo=timezone.utc).timestamp())
    assert service.calls == 0
    with factory() as session:
        assert session.query(Job).count() == 0
    engine.dispose()


def test_former_roster_day_uses_regular_schedule_and_batch(tmp_path, monkeypatch):
    engine, factory, service = setup(tmp_path)
    service.config = Settings(_env_file=None, runtime_dir=tmp_path, coordination_required=False)
    update_settings(factory, {'schedule_enabled': True})
    monkeypatch.setattr(Worker, '_morning_log', lambda *args: None)
    worker = Worker(factory, service)
    worker.tick(datetime(2026, 11, 25, 23, tzinfo=timezone.utc).timestamp())
    assert service.calls == 0
    worker.tick(datetime(2026, 11, 26, 1, tzinfo=timezone.utc).timestamp())
    assert service.calls == 1
    with factory() as session:
        job = session.query(Job).one()
        assert job.state == 'done' and job.batch_id == 'batch-result'
    engine.dispose()


def test_retired_roster_resume_cannot_become_regular_price_submission(tmp_path):
    engine, factory, service = setup(tmp_path)
    job_id = enqueue(factory, 'run', {'date': '2026-11-26', 'resume_roster_workflow': 'a' * 32},
                     'daily:2026-11-26')
    Worker(factory, service).tick(datetime(2026, 11, 26, 1, tzinfo=timezone.utc).timestamp())
    assert service.calls == 0
    with factory() as session:
        assert session.get(Job, job_id).state == 'cancelled'
    engine.dispose()
