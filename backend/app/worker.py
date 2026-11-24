"""持久后台任务进程。日志仅提供技术排查，不改变租约、执行意图或未知不重传规则。"""
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4
import time
from sqlalchemy import update, or_
from sqlalchemy.exc import IntegrityError
from app.db import transaction
from app.models import Job, WorkerLease
from app.services.store import claim_job, enqueue, settings_value, stamp, BEIJING
from app.adapters.base import SourceUnavailable
from app.observability import bind_context, bind_sink, event, get_sink


class Worker:
    def __init__(self, factory, service):
        self.factory, self.service = factory, service
        self.owner = str(uuid4())
        self.current_job = None
        self.last_reconcile = -1e10
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
        now = time.time() if now is None else now
        acquired = self.acquire(now)
        if acquired != self._held_lease:
            event('worker.acquired' if acquired else 'worker.lease_lost')
            self._held_lease = acquired
        if not acquired:
            return
        if getattr(getattr(self.service, "config", None), "coordination_required", False):
            from app.services.notifications import dispatch_notices
            try:
                dispatch_notices(self.factory, self.service.config)
            except Exception as exc:
                # 通知投递有自己的持久状态。故障不影响价格核验和已排队的取数任务。
                self._failure("worker.notice_dispatch_failed", exc)
            else:
                self._failures.pop("worker.notice_dispatch_failed", None)
        config = settings_value(self.factory)
        local = datetime.fromtimestamp(now, timezone.utc).astimezone(BEIJING)
        # 2026-11-24用户确认：北京时间09:00起建立当日唯一任务，晚启动仍补建。
        service_config = getattr(self.service, "config", None)
        pilot_begins_today = (getattr(service_config, "erp_roster_schedule_enabled", False)
                              and getattr(service_config, "erp_roster_date", None) == local.date())
        if (config["schedule_enabled"] or pilot_begins_today) and (local.hour, local.minute) >= (9, 0):
            enqueue(self.factory, "run", {"date": local.date().isoformat()}, f"daily:{local.date().isoformat()}")
        if now - self.last_reconcile >= 60:
            self.service.reconcile()
            self.last_reconcile = now
        job = claim_job(self.factory, self.owner, now)
        if job is None:
            return
        metadata = job.payload.get('_log_context', {})
        metadata = metadata if isinstance(metadata, dict) else {}
        with bind_context(job_id=job.id, **{k: metadata[k] for k in ('request_id', 'operation_id') if k in metadata}):
            self._run_job(job, now, tick_started)

    def _run_job(self, job, now, tick_started):
        self.current_job = job.id
        started = time.monotonic()
        event('job.started', kind=job.kind)
        state, error, batch_id, retry = "done", None, None, 0
        try:
            scheduled_day = job.payload.get("date")
            current_time = now + (time.monotonic() - tick_started)
            current_day = datetime.fromtimestamp(current_time, timezone.utc).astimezone(BEIJING).date().isoformat()
            daily = job.kind == "run" and scheduled_day and job.dedupe_key == f"daily:{scheduled_day}"
            service_config = getattr(self.service, "config", None)
            roster_date = getattr(service_config, "erp_roster_date", None)
            if daily and scheduled_day < current_day:
                # 日任务只消费本日数据。旧执行继续走独立核验，新日由其自己的唯一日任务处理。
                state, error = "cancelled", "原定时任务已过业务日，按新业务日定时任务处理"
            elif daily and roster_date and scheduled_day == roster_date.isoformat():
                from app.services.erp_workflow import run_workflow
                from app.services.erp_roster_pricing import run_roster_pricing
                from app.services.erp_capture_store import write_verified
                # 采集只读，再交给真实定价服务执行已有的提交和回读检查。
                config = service_config.model_copy(update={'live_submit_enabled': False})
                capture = run_workflow(config, roster_date=roster_date)
                if (service_config.adapter_mode == 'live' and service_config.live_submit_enabled
                        and capture['ready_prices']):
                    capture['pricing_batches'] = run_roster_pricing(self.service, job.id, roster_date, capture)
                    write_verified(Path(capture['artifact_dir']) / 'result.json', capture)
                from app.services.roster_notices import enqueue_roster_notices
                try:
                    enqueue_roster_notices(self.factory, config, roster_date, capture)
                except Exception as exc:
                    # 消息入队失败不影响结果存档，也不能因此重新采集。
                    error_id = event('worker.roster_notice_enqueue_failed', exception=exc)
                    error = f"固定名单钉钉通知入队失败，排查编号：{error_id}"
                log_error = self._morning_log(config, roster_date, capture=capture)
                error = "；".join(filter(None, (error, log_error))) or None
            elif job.kind == "erp_roster_notice":
                from app.services.roster_notices import send_roster_notice
                result = send_roster_notice(self.factory, service_config, job.id, self.owner)
                if result['state'] != 'accepted':
                    state, error = 'failed', result['error']
            elif job.kind == "run":
                batch_id = (self.service.run_batch(job.id, item_id=job.payload["item_id"])
                            if job.payload.get("item_id") else self.service.run_batch(job.id))
                if daily and getattr(service_config, "adapter_mode", None) == "live":
                    error = self._morning_log(service_config, date.fromisoformat(scheduled_day), batch_id=batch_id)
            elif job.kind == "erp_reconciliation":
                from app.services.erp_pricing import save_preview_reconciliation
                batch_id = save_preview_reconciliation(
                    self.factory, self.service.config, job.payload["capture_id"], job.payload["preview_id"],
                    job.payload["artifact_sha256"])
            elif job.kind == "reconcile":
                self.service.reconcile()
            elif job.kind == "correction":
                from app.services.corrections import run_correction
                batch_id = run_correction(self.service, job.payload["correction_id"])
            else:
                raise ValueError("未知任务类型")
        except SourceUnavailable as exc:
            # 取数和本轮核验都可能耗时，等待从失败时起算。
            failed_at = now + (time.monotonic() - tick_started)
            state, error, retry = "queued", "当日数据源尚未就绪，5分钟后重新检查", failed_at + 300
            error_id = event('job.waiting', exception=exc)
            if error_id:
                error += f"；排查编号：{error_id}"
        except Exception as exc:
            state = "failed"
            if job.kind == "erp_reconciliation":
                error = "文件对账入批失败，请核对报告和运行目录"
            else:
                error = f"任务失败（{type(exc).__name__}），已提交记录仍保持独立核验"
            error_id = event('job.failed', exception=exc)
            if error_id:
                error += f"；排查编号：{error_id}"
        finally:
            try:
                with transaction(self.factory) as session:
                    session.execute(update(Job).where(Job.id == job.id, Job.owner == self.owner).values(
                        state=state, error=error, batch_id=batch_id, lease_until=retry))
                    session.execute(update(WorkerLease).where(WorkerLease.name == "worker", WorkerLease.owner == self.owner)
                        .values(last_error=error, heartbeat=stamp()))
                event('job.completed', state=state, batch_id=batch_id, duration_ms=(time.monotonic() - started) * 1000)
            finally:
                self.current_job = None

    def _morning_log(self, config, day, **source):
        from app.services.morning_log import publish_morning_log, save_morning_log, sync_cancellation_log
        try:
            filename = save_morning_log(config, day, factory=self.factory, **source)
            publish_morning_log(config, filename)
        except Exception as exc:
            # 采集/执行已完成，日志失败不能触发再次取数或提交。
            error_id = event('worker.morning_log_failed', exception=exc)
            error = f"上午日志未写入钉盘，请按排查编号检查：{error_id}"
        else:
            error = None
        try:
            sync_cancellation_log(config, day)
        except Exception as exc:
            event('worker.cancellation_log_failed', exception=exc)
        return error

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
                        self._failure('worker.tick_failed', exc)
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
        app.state.engine.dispose()
        with bind_sink(app.state.log_sink):
            event('app.stopped')


if __name__ == "__main__":
    main()
