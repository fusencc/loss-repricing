from datetime import datetime, timezone
from app.db import Base, create_database
from app.models import Job
from app.services.store import enqueue, claim_job


def database(tmp_path):
    engine, factory = create_database(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return engine, factory


def test_same_daily_trigger_has_one_persistent_job(tmp_path):
    engine, factory = database(tmp_path)
    first = enqueue(factory, "run", {"business_date": "2026-11-05"}, "daily:2026-11-05")
    second = enqueue(factory, "run", {}, "daily:2026-11-05")
    assert first == second and isinstance(first, str)
    with factory() as session:
        assert session.query(Job).count() == 1
    engine.dispose()


def test_running_job_not_claimed_twice_and_expired_lease_recovers(tmp_path):
    engine, factory = database(tmp_path)
    job_id = enqueue(factory, "run", {}, "daily")
    assert claim_job(factory, "worker-a", 100).id == job_id
    assert claim_job(factory, "worker-b", 101) is None
    assert claim_job(factory, "worker-b", 1000).id == job_id
    engine.dispose()
