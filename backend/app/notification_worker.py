"""真实通知专用进程；不加载调价适配器，不领取或执行价格任务。"""
import json
import signal
from threading import Event
from sqlalchemy import select
from sqlalchemy.engine import make_url
from app.config import get_settings
from app.db import Base, create_database
from app.models import Batch
from app.services.notifications import dispatch_notices, write_batch_notices
from app.services.store import stamp


def notification_tick(factory, config):
    # 持久批次即投递意图来源；覆盖人工实时入档路径，demo/file 不进入真实发送。
    with factory.begin() as session:
        for batch_id in session.scalars(select(Batch.id).where(Batch.mode == 'live')):
            write_batch_notices(session, batch_id)
    return dispatch_notices(factory, config)


def main():
    import fcntl  # 常驻锁仅在 WSL 入口使用；通用通知函数仍可在 Windows 测试。
    config=get_settings()
    if config.notification_mode != 'live' or not config.ding_enabled:
        raise SystemExit('真实机器人通知未启用')
    archive=config.live_history_database_url.get_secret_value()
    url=make_url(archive)
    if url.get_backend_name() != 'sqlite' or not url.database:
        raise SystemExit('本机通知服务必须绑定明确的真实档案数据库')
    from pathlib import Path
    if not Path(url.database).is_file():
        raise SystemExit('真实档案不存在')
    lock=open(config.runtime_dir / 'notification-worker.lock','w')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    engine,factory=create_database(archive)
    Base.metadata.create_all(engine)
    stop=Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_:stop.set())
    health=config.runtime_dir / 'notification-worker-health.json'
    started=stamp()
    try:
        while not stop.is_set():
            try:
                result=notification_tick(factory,config)
                state={'state':'running','started_at':started,'heartbeat':stamp(),**result}
            except Exception as exc:
                # 异常正文可能包含业务数据或认证字段，只记类型；独立条目状态仍由 outbox 保存。
                state={'state':'error','started_at':started,'heartbeat':stamp(),'error_type':type(exc).__name__}
            temporary=health.with_suffix('.tmp')
            temporary.write_text(json.dumps(state,ensure_ascii=False))
            temporary.replace(health)
            stop.wait(max(5,config.worker_poll_seconds))
    finally:
        engine.dispose()
        lock.close()


if __name__ == '__main__':
    main()
