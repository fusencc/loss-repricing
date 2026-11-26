from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.adapters.base import SourceUnavailable
from app.adapters.dingtalk import DingTalkClient
from app.config import Settings
from app.db import Base, create_database
from app.models import Job, Notice
from app.services.notifications import dispatch_notices
from app.services.operation_alerts import enqueue_operation_alert, watch_operations
from app.services.store import claim_job, enqueue, update_settings
from app.worker import Worker


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    engine, factory = create_database(f'sqlite:///{tmp_path / "alerts.db"}')
    Base.metadata.create_all(engine)
    config = Settings(_env_file=None, runtime_dir=tmp_path, ding_enabled=True,
                      ding_client_id='robot', ding_client_secret='secret', ding_alert_userid='only-admin')
    send = Mock(return_value={'state': 'unknown'})
    monkeypatch.setattr(DingTalkClient, 'send_notice', send)
    yield factory, config, send
    engine.dispose()


def test_missing_daily_at_eight_is_deduped_and_goes_only_to_admin(runtime, monkeypatch):
    factory, config, send = runtime
    now = datetime(2026, 11, 26, 0, tzinfo=timezone.utc)
    monkeypatch.setattr('app.services.notifications.utcnow', lambda: now)
    dispatch_notices(factory, config)
    send.assert_not_called()
    update_settings(factory, {'schedule_enabled': True})
    now -= timedelta(seconds=1)
    dispatch_notices(factory, config)
    send.assert_not_called()
    now += timedelta(seconds=1)
    dispatch_notices(factory, config)
    dispatch_notices(factory, config)
    send.assert_called_once()
    userid, title, text, _ = send.call_args.args
    assert userid == 'only-admin' and title == '亏损提价运行告警' and '未看到任务入队' in text
    with factory() as session:
        notice = session.scalars(select(Notice)).one()
        assert notice.batch_id is None and notice.recipient_id == 'admin'
        assert notice.delivery_state == 'unknown' and notice.summary['code'] == 'daily_missing'


def test_watch_operations_distinguishes_long_running_expired_and_not_started(runtime):
    factory, _, _ = runtime
    now = datetime(2026, 11, 26, 0, tzinfo=timezone.utc)
    running = enqueue(factory, 'run', {})
    expired = enqueue(factory, 'run', {})
    for _ in range(2):
        claim_job(factory, 'worker', (now - timedelta(minutes=70)).timestamp())
    waiting = enqueue(factory, 'run', {'date': '2026-11-26'}, 'daily:2026-11-26')
    update_settings(factory, {'schedule_enabled': True})
    with factory.begin() as session:
        session.get(Job, running).lease_until = now.timestamp() + 120
    watch_operations(factory, now)
    watch_operations(factory, now + timedelta(seconds=5))
    with factory() as session:
        assert {n.summary['code']: n.summary['job_id'] for n in session.scalars(select(Notice))} == {
            'stalled': running, 'interrupted': expired, 'daily_queued': waiting,
        }
        assert session.get(Job, running).state == 'running'
        assert session.get(Job, expired).state == 'running'
        assert session.get(Job, waiting).state == 'queued'


@pytest.mark.parametrize('mode', ['live', 'file'])
def test_worker_alerts_preserve_source_retry_and_final_failure(runtime, mode):
    factory, config, _ = runtime
    config.adapter_mode = mode
    class Service:
        def reconcile(self):
            pass
        def run_batch(self, job_id):
            if not self.ready:
                raise SourceUnavailable('do not send raw response')
            raise RuntimeError('do not send raw response')
    service = Service()
    service.config = config
    service.ready = False
    worker = Worker(factory, service)
    job_id = enqueue(factory, 'run', {})
    worker.tick(100)
    worker.tick(401)
    with factory() as session:
        assert session.get(Job, job_id).state == 'queued'
    service.ready = True
    worker.tick(702)
    with factory() as session:
        assert session.get(Job, job_id).state == 'failed'
        assert sorted(n.summary['code'] for n in session.scalars(select(Notice))) == (
            ['job_failed', 'source_waiting'] if mode == 'live' else [])


def test_operation_waits_until_eight_and_never_falls_back_to_sales(runtime, monkeypatch):
    factory, config, send = runtime
    now = datetime(2026, 11, 25, 23, 59, tzinfo=timezone.utc)
    monkeypatch.setattr('app.services.notifications.utcnow', lambda: now)
    with factory.begin() as session:
        enqueue_operation_alert(session, 'archive_failed', 'job', now, job_id='job')
    dispatch_notices(factory, config)
    now += timedelta(minutes=1)
    config.adapter_mode = 'file'
    dispatch_notices(factory, config)
    config.adapter_mode = 'live'
    config.ding_alert_userid = ''
    dispatch_notices(factory, config)
    send.assert_not_called()
    with factory() as session:
        notice = session.scalars(select(Notice)).one()
        assert notice.delivery_state == 'pending' and '管理员' in notice.last_error
    config.ding_alert_userid = 'only-admin'
    dispatch_notices(factory, config)
    send.assert_called_once()
    assert send.call_args.args[0] == 'only-admin' and '钉盘' in send.call_args.args[2]


def test_notification_loop_keeps_running_while_price_job_waits(runtime, monkeypatch):
    factory, config, _ = runtime
    entered_job, notified = Event(), Event()

    class StopWorker(BaseException):
        pass

    class Service:
        def reconcile(self):
            pass

        def run_batch(self, job_id):
            entered_job.set()
            assert notified.wait(5), 'notification polling waited for the price job'
            raise StopWorker

    service = Service()
    service.config = config
    worker = Worker(factory, service)
    monkeypatch.setattr(worker, '_notification_tick', lambda: entered_job.is_set() and notified.set())
    enqueue(factory, 'run', {})
    with pytest.raises(StopWorker):
        worker.run_forever(poll_seconds=0.01)
    assert notified.is_set()
