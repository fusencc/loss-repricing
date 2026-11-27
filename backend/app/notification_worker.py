"""独立投递真实通知，不加载调价适配器，也不领取价格任务。"""
import json
import signal
from pathlib import Path
from threading import Event

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db import create_database
from app.observability import bind_sink, configure_logging, event
from app.services.notifications import dispatch_notices
from app.services.store import stamp


def main():
    import fcntl

    config = get_settings()
    archive = config.live_history_database_url.get_secret_value() or (
        config.database_url if config.adapter_mode == 'live' else '')
    if not archive:
        raise SystemExit('通知服务需绑定真实提价档案')
    url = make_url(archive)
    if url.get_backend_name() == 'sqlite' and (not url.database or not Path(url.database).is_file()):
        raise SystemExit('真实提价档案尚未建立')

    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    sink = configure_logging(config, 'worker')
    with (config.runtime_dir / 'notification-worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        engine, factory = create_database(archive)
        stop = Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        try:
            with bind_sink(sink):
                _serve(factory, config, stop)
        finally:
            engine.dispose()


def _serve(factory, config, stop):
    health = config.runtime_dir / 'notification-worker-health.json'
    started = stamp()
    while not stop.is_set():
        try:
            result = dispatch_notices(factory, config)
            state = {'state': 'running', 'started_at': started, 'heartbeat': stamp(), **result}
        except Exception as exc:
            event('worker.notice_dispatch_failed', exception=exc)
            state = {'state': 'error', 'started_at': started, 'heartbeat': stamp(), 'error_type': type(exc).__name__}
        temporary = health.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        temporary.replace(health)
        stop.wait(max(5, config.worker_poll_seconds))


if __name__ == '__main__':
    main()
