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
    job_id = enqueue(factory, "run", {})
    worker_b.tick(101)
    assert service.calls == 0
    worker_b.tick(300)
    assert service.calls == 1
    engine.dispose()
