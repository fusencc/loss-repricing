"""持久后台任务进程。定时触发、外部来源未就绪重试、租约恢复均不依赖网页开着。"""
from datetime import datetime, timezone
from threading import Event, Thread
from uuid import uuid4
import time
from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError
from app.db import transaction
from app.models import Job, WorkerLease
from app.services.store import claim_job, enqueue, settings_value, stamp, BEIJING
from app.adapters.base import SourceUnavailable


class Worker:
    def __init__(self, factory, service):
        self.factory, self.service = factory, service
        self.owner = str(uuid4())
        self.current_job = None
        self.last_reconcile = -1e10

    def acquire(self, now):
        try:
            with transaction(self.factory) as session:
                if session.get(WorkerLease, "worker") is None:
                    session.add(WorkerLease(name="worker", owner=self.owner, expires=now + 120, heartbeat=stamp()))
                    return True
        except IntegrityError:
            pass
        with transaction(self.factory) as session:
            result = session.execute(update(WorkerLease).where(WorkerLease.name == "worker",
                or_(WorkerLease.owner == self.owner, WorkerLease.expires < now)).values(
                    owner=self.owner, expires=now + 120, heartbeat=stamp()))
            return result.rowcount == 1

    def heartbeat(self):
        now = time.time()
        with transaction(self.factory) as session:
            result = session.execute(update(WorkerLease).where(WorkerLease.name == "worker", WorkerLease.owner == self.owner)
                .values(expires=now + 120, heartbeat=stamp()))
            if result.rowcount and self.current_job:
                session.execute(update(Job).where(Job.id == self.current_job, Job.owner == self.owner, Job.state == "running")
                    .values(lease_until=now + 120))

    def tick(self, now=None):
        now = time.time() if now is None else now
        if not self.acquire(now):
            return
        config = settings_value(self.factory)
        local = datetime.fromtimestamp(now, timezone.utc).astimezone(BEIJING)
        if config["schedule_enabled"] and local.hour >= 8:
            enqueue(self.factory, "run", {"date": local.date().isoformat()}, f"daily:{local.date().isoformat()}")
        if now - self.last_reconcile >= 60:
            self.service.reconcile()
            self.last_reconcile = now
        job = claim_job(self.factory, self.owner, now)
        if job is None:
            return
        self.current_job = job.id
        state, error, batch_id, retry = "done", None, None, 0
        try:
            if job.kind == "run":
                batch_id = (self.service.run_batch(job.id, item_id=job.payload["item_id"])
                            if job.payload.get("item_id") else self.service.run_batch(job.id))
            elif job.kind == "reconcile":
                self.service.reconcile()
            else:
                raise ValueError("未知任务类型")
        except SourceUnavailable:
            state, error, retry = "queued", "当日数据源尚未就绪，5分钟后重新检查", now + 300
        except Exception as exc:
            state, error = "failed", f"任务失败（{type(exc).__name__}），已提交记录仍保持独立核验"
        finally:
            with transaction(self.factory) as session:
                session.execute(update(Job).where(Job.id == job.id, Job.owner == self.owner).values(
                    state=state, error=error, batch_id=batch_id, lease_until=retry))
                session.execute(update(WorkerLease).where(WorkerLease.name == "worker", WorkerLease.owner == self.owner)
                    .values(last_error=error, heartbeat=stamp()))
            self.current_job = None

    def run_forever(self, poll_seconds=5):
        stop = Event()
        def renew():
            while not stop.wait(20):
                try:
                    self.heartbeat()
                except Exception:
                    # 数据库故障不会触发任何直接外部重传；主线程下次持久事务也会失败。
                    pass
        thread = Thread(target=renew, daemon=True)
        thread.start()
        try:
            while True:
                try:
                    self.tick()
                except Exception as exc:
                    print(f"worker tick error: {type(exc).__name__}", flush=True)
                time.sleep(poll_seconds)
        finally:
            stop.set()
            thread.join(timeout=2)


def main():
    from app.main import create_app
    app = create_app()
    Worker(app.state.factory, app.state.service).run_forever(app.state.config.worker_poll_seconds)


if __name__ == "__main__":
    main()
