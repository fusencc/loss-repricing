"""Live worker alerts share the DingTalk outbox, independently of sales ownership."""
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Job, Setting
from app.models.notifications import Notice
from app.services.store import BEIJING, stamp


TITLE = '亏损提价运行告警'
MESSAGES = {
    'daily_missing': '今天的定时任务已启用，但北京时间 08:00 后仍未看到任务入队。请检查后台进程和定时设置。',
    'daily_queued': '今天的定时任务已入队，但北京时间 08:00 后仍未开始。请检查后台进程和队列。',
    'stalled': '任务运行超过 60 分钟仍未结束，可能卡住。请按任务编号查看最近进度。',
    'interrupted': '运行中的任务已失去后台进程心跳。请检查进程状态及该任务的最近进度。',
    'source_waiting': '任务未能取得所需数据，已进入后台重试。',
    'job_failed': '后台任务失败；已有价格提交仍按原流程独立核验。',
    'archive_failed': '至少一处钉盘运行日志写入未完成，将按原流程补写。',
    'worker_error': '后台轮询发生异常，请检查取数任务、价格核验和后台进程日志。',
}


def enqueue_operation_alert(session, code, reference, at, *, job_id=None, error_id=None):
    key = f'operation:{code}:{reference}'
    if session.scalar(select(Notice.id).where(Notice.dedupe_key == key)):
        return
    try:
        with session.begin_nested():
            session.add(Notice(id=str(uuid4()), dedupe_key=key, recipient_id='admin',
                               summary={'kind':'operation', 'title':TITLE, 'code':code,
                                        'job_id':job_id, 'error_id':error_id, 'occurred_at':stamp(at)},
                               item_ids=[], delivery_state='pending', created_at=stamp(at), updated_at=stamp(at)))
    except IntegrityError:
        if not session.scalar(select(Notice.id).where(Notice.dedupe_key == key).with_for_update()):
            raise


def watch_operations(factory, now):
    local = now.astimezone(BEIJING)
    with factory.begin() as session:
        if local.hour >= 8:
            settings = session.get(Setting, 'runtime')
            day = local.date().isoformat()
            if settings and settings.value.get('schedule_enabled'):
                daily = session.scalar(select(Job).where(Job.dedupe_key == f'daily:{day}'))
                if daily is None:
                    enqueue_operation_alert(session, 'daily_missing', day, now)
                elif daily.state == 'queued' and not daily.payload.get('_attempt_started'):
                    enqueue_operation_alert(session, 'daily_queued', daily.id, now, job_id=daily.id)
        for job in session.scalars(select(Job).where(Job.state == 'running')).all():
            # Ignore attempts already recovered by the price worker; the lease is never changed here.
            if job.lease_until <= now.timestamp():
                enqueue_operation_alert(session, 'interrupted', job.id, now, job_id=job.id)
            elif (started := job.payload.get('_attempt_started')) and now - datetime.fromisoformat(started) >= timedelta(hours=1):
                enqueue_operation_alert(session, 'stalled', job.id, now, job_id=job.id)


def operation_card(summary):
    at = datetime.fromisoformat(summary['occurred_at']).astimezone(BEIJING)
    lines = [f'**{TITLE}**', f'发现时间（北京时间）：{at:%Y-%m-%d %H:%M:%S}', MESSAGES[summary['code']]]
    if summary.get('job_id'):
        lines.append(f"任务编号：`{summary['job_id']}`")
    if summary.get('error_id'):
        lines.append(f"排查编号：`{summary['error_id']}`")
    return '\n\n'.join(lines)
