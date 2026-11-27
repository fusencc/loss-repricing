"""持久后台任务进程。日志仅提供技术排查，不改变租约、执行意图或未知不重传规则。"""
from datetime import date, datetime, timezone
from threading import Event, Thread
from uuid import uuid4
import time
from sqlalchemy import or_, select, true, update
from sqlalchemy.exc import IntegrityError
from app.db import transaction
from app.models import Job, WorkerLease
from app.services.store import claim_job, enqueue, settings_value, stamp, utcnow, BEIJING
from app.services.operation_alerts import enqueue_operation_alert
from app.services.scheduled_departments import run_daily_departments
from app.adapters.base import SourceUnavailable
from app.observability import bind_context, bind_sink, event, get_sink


class Worker:
    def __init__(self, factory, service):
        self.factory, self.service = factory, service
        self.owner = str(uuid4())
        self.current_job = None
        self.last_archive_refresh = -1e10
        self._held_lease = False
        self._failures = {}
        self.log_sink = get_sink(getattr(service, 'config', None), 'worker')

    def _failure(self, name, exc):
        now = time.monotonic()
        previous, count = self._failures.get(name, (-1e10, 0))
        if now - previous >= 60:
            event(name, exception=exc, job_id=self.current_job)
            if count:
                event('worker.repeated', suppressed=count)
            self._failures[name] = (now, 0)
        else:
            self._failures[name] = (previous, count + 1)

    def _alert(self, code, reference, *, job_id=None, error_id=None):
        if getattr(getattr(self.service, 'config', None), 'adapter_mode', None) != 'live':
            return
        try:
            with transaction(self.factory) as session:
                enqueue_operation_alert(session, code, reference, utcnow(), job_id=job_id, error_id=error_id)
        except Exception as exc:
            self._failure('worker.notice_dispatch_failed', exc)

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
        with bind_sink(self.log_sink):
            return self._tick(now)

    def _tick(self, now=None):
        tick_started = time.monotonic()
        use_wall_clock = now is None
        now = time.time() if use_wall_clock else now
        acquired = self.acquire(now)
        if acquired != self._held_lease:
            event('worker.acquired' if acquired else 'worker.lease_lost')
            self._held_lease = acquired
        if not acquired:
            return
        config = settings_value(self.factory)
        local = datetime.fromtimestamp(now, timezone.utc).astimezone(BEIJING)
        service_config = getattr(self.service, "config", None)
        if config["schedule_enabled"] and local.hour >= 5:
            enqueue(self.factory, "run", {"date": local.date().isoformat()}, f"daily:{local.date().isoformat()}")
        archive_due = now - self.last_archive_refresh >= 60
        archive_jobs = []
        if archive_due and service_config and self._archive_enabled(service_config):
            with self.factory() as session:
                archive_jobs = list(session.scalars(select(Job.id).where(
                    Job.state == 'done', Job.payload['archive_pending'].as_boolean() == true()).order_by(Job.created_at)))
        job = claim_job(self.factory, self.owner, time.time() if use_wall_clock else now)
        if job is not None:
            metadata = job.payload.get('_log_context', {})
            metadata = metadata if isinstance(metadata, dict) else {}
            with bind_context(job_id=job.id, **{k: metadata[k] for k in ('request_id', 'operation_id') if k in metadata}):
                try:
                    if self._run_job(job, now, tick_started):
                        archive_jobs.append(job.id)
                finally:
                    self.current_job = None
        for job_id in archive_jobs:
            with bind_context(job_id=job_id):
                self._archive_job(service_config, job_id)
        if archive_due:
            self.last_archive_refresh = now + (time.monotonic() - tick_started)

    def _run_job(self, job, now, tick_started):
        self.current_job = job.id
        started = time.monotonic()
        event('job.started', kind=job.kind)
        state, error, batch_id, retry = "done", None, None, 0
        alert, error_id = None, None
        archive_pending = False
        try:
            scheduled_day = job.payload.get("date")
            current_time = now + (time.monotonic() - tick_started)
            current_day = datetime.fromtimestamp(current_time, timezone.utc).astimezone(BEIJING).date().isoformat()
            daily = job.kind == "run" and scheduled_day and job.dedupe_key == f"daily:{scheduled_day}"
            service_config = getattr(self.service, "config", None)
            if daily and scheduled_day < current_day:
                # 日任务只消费本日数据。旧执行继续走独立核验，新日由其自己的唯一日任务处理。
                state, error = "cancelled", "原定时任务已过业务日，按新业务日定时任务处理"
            elif job.payload.get('resume_roster_workflow') or job.kind == 'erp_roster_notice':
                state, error = 'cancelled', '固定名单任务已停用'
            elif job.kind == "run":
                if daily and getattr(getattr(self.service, "adapter", None), "mode", None) == "live":
                    batch_id = run_daily_departments(self.service, job, date.fromisoformat(scheduled_day))
                elif job.payload.get("item_id"):
                    batch_id = self.service.run_batch(job.id, item_id=job.payload["item_id"])
                else:
                    batch_id = self.service.run_batch(job.id)
                archive_pending = bool(daily and getattr(service_config, 'adapter_mode', None) == 'live'
                                       and self._archive_enabled(service_config))
            elif job.kind == "erp_reconciliation":
                from app.services.erp_pricing import save_preview_reconciliation
                batch_id = save_preview_reconciliation(
                    self.factory, self.service.config, job.payload["capture_id"], job.payload["preview_id"],
                    job.payload["artifact_sha256"])
            elif job.kind == "reconcile":
                self.service.reconcile(tasks_only=True)
            elif job.kind == "correction":
                from app.services.corrections import run_correction
                batch_id = run_correction(self.service, job.payload["correction_id"])
            else:
                raise ValueError("未知任务类型")
        except SourceUnavailable as exc:
            # 取数和本轮核验都可能耗时，等待从失败时起算。
            failed_at = now + (time.monotonic() - tick_started)
            state, error, retry = "queued", "当日数据源尚未就绪，5分钟后重新检查", failed_at + 300
            alert = 'source_waiting'
            error_id = event('job.waiting', exception=exc)
            if error_id:
                error += f"；排查编号：{error_id}"
        except Exception as exc:
            state = "failed"
            alert = 'job_failed'
            if job.kind == "erp_reconciliation":
                error = "文件对账入批失败，请核对报告和运行目录"
            else:
                error = f"任务失败（{type(exc).__name__}），已提交记录仍保持独立核验"
            error_id = event('job.failed', exception=exc)
            if error_id:
                error += f"；排查编号：{error_id}"
        with transaction(self.factory) as session:
            current = session.get(Job, job.id, with_for_update=True)
            if current.owner != self.owner:
                return False
            current.state, current.error, current.batch_id, current.lease_until = state, error, batch_id, retry
            if archive_pending:
                current.payload = {**current.payload, 'archive_pending': True}
            session.execute(update(WorkerLease).where(WorkerLease.name == "worker", WorkerLease.owner == self.owner)
                .values(last_error=error, heartbeat=stamp()))
        event('job.completed', state=state, batch_id=batch_id, duration_ms=(time.monotonic() - started) * 1000)
        if alert:
            self._alert(alert, job.id, job_id=job.id, error_id=error_id)
        return archive_pending

    @staticmethod
    def _archive_enabled(config):
        return bool(config.ding_tk_morning_log_folder or config.ding_cancellation_log_folder)

    def _archive_job(self, config, job_id):
        with self.factory() as session:
            day = date.fromisoformat(session.get(Job, job_id).payload['date'])
        error = self._morning_log(config, day)
        with transaction(self.factory) as session:
            current = session.get(Job, job_id, with_for_update=True)
            current.error = error
            if error is None:
                current.payload = {key: value for key, value in current.payload.items() if key != 'archive_pending'}
        if error:
            self._alert('archive_failed', job_id, job_id=job_id)

    def _morning_log(self, config, day):
        from app.services.morning_log import publish_morning_log, save_morning_log, save_success_log
        errors = []
        for success, save, folder in (
                (False, save_morning_log, config.ding_tk_morning_log_folder),
                (True, save_success_log, config.ding_cancellation_log_folder)):
            if not folder:
                continue
            try:
                filename = save(config, day, factory=self.factory)
                publish_morning_log(config, filename, success=success)
            except Exception as exc:
                # 两处独立归档；日志失败不能重采订单或重传价格，也不能跳过另一处。
                error_id = event('worker.cancellation_log_failed' if success else 'worker.morning_log_failed', exception=exc)
                errors.append(f"{'成功订单清单' if success else '完整提价日志'}未写入钉盘，排查编号：{error_id}")
        return '；'.join(errors) or None

    def run_forever(self, poll_seconds=5):
        stop = Event()
        def renew():
            with bind_sink(self.log_sink):
                while not stop.wait(20):
                    try:
                        self.heartbeat()
                        if 'worker.heartbeat_failed' in self._failures:
                            _, count = self._failures.pop('worker.heartbeat_failed')
                            event('worker.heartbeat_recovered', suppressed=count)
                    except Exception as exc:
                        # 数据库故障不会触发直接外部重传，保留错误位置而非吞掉技术证据。
                        self._failure('worker.heartbeat_failed', exc)
        thread = Thread(target=renew, daemon=True)
        thread.start()
        try:
            with bind_sink(self.log_sink):
                while True:
                    try:
                        self.tick()
                        self._failures.pop('worker.tick_failed', None)
                    except Exception as exc:
                        first_failure = 'worker.tick_failed' not in self._failures
                        self._failure('worker.tick_failed', exc)
                        if first_failure:
                            self._alert('worker_error', utcnow().astimezone(BEIJING).date().isoformat())
                    time.sleep(poll_seconds)
        finally:
            stop.set()
            thread.join(timeout=2)


def main():
    from app.main import create_app
    app = create_app(log_role='worker')
    try:
        Worker(app.state.factory, app.state.service).run_forever(app.state.config.worker_poll_seconds)
    finally:
        try:
            app.state.service.close()
        finally:
            app.state.engine.dispose()
            with bind_sink(app.state.log_sink):
                event('app.stopped')


if __name__ == "__main__":
    main()
