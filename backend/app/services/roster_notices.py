"""固定名单的只读算价摘要，按 ERP 采集范围中的姓名定向投递。"""
from sqlalchemy import select

from app.adapters.dingtalk import DingTalkClient
from app.db import transaction
from app.models import Job
from app.services.store import enqueue, stamp


SCOPE_STATUS = {
    'completed': '完成',
    'needs_attention': '有待核对',
    'blocked': '未完成',
    'failed': '失败',
}


def enqueue_roster_notices(factory, config, day, capture):
    if not config.ding_enabled or config.adapter_mode != 'live':
        return
    for name in capture['salespersons']:
        scopes = [
            {key: row[key] for key in ('department', 'status', 'ready_prices')}
            for row in capture['departments'] if row['salesperson'] == name
        ]
        if not scopes:
            continue
        enqueue(factory, 'erp_roster_notice',
                {'date': day.isoformat(), 'salesperson': name, 'scopes': scopes},
                f'erp-roster-notice:{day.isoformat()}:{name}')


def send_roster_notice(factory, config, job_id, owner):
    with transaction(factory) as session:
        job = session.scalar(select(Job).where(Job.id == job_id, Job.owner == owner).with_for_update())
        if job is None:
            return {'state': 'failed', 'error': '通知任务的租约已失效'}
        payload = job.payload
        previous = payload.get('delivery')
        if previous:
            if previous['state'] != 'sending':
                return previous
            result = dict(previous, state='unknown', error='发送过程中断，结果未知，禁止自动重发')
            job.payload = dict(payload, delivery=result)
            return result

        name = payload['salesperson']
        userid = config.ding_roster_userids.get(name, '')
        if not (config.adapter_mode == 'live' and config.ding_enabled
                and config.ding_client_id and config.ding_client_secret):
            result = {'state': 'failed', 'error': '钉钉机器人未启用或应用配置不完整'}
        elif not userid.strip() or list(config.ding_roster_userids.values()).count(userid) != 1:
            result = {'state': 'failed', 'error': '销售员姓名未配置唯一钉钉 User ID'}
        else:
            result = {'state': 'sending', 'userid': userid, 'at': stamp()}
        job.payload = dict(payload, delivery=result)

    if result['state'] != 'sending':
        return result
    # 先持久化发送意图，再调用钉钉。租约恢复后只记录未知，不重发。
    scopes = payload['scopes']
    ready = sum(scope['ready_prices'] for scope in scopes)
    title = '亏损提价算价结果'
    rows = [
        f"- {scope['department']}：{SCOPE_STATUS.get(scope['status'], '状态待核对')}，"
        f"算价通过 {scope['ready_prices']} 条"
        for scope in scopes
    ]
    markdown = (f"**{title}**\n\n{payload['date']} · {name} · 算价通过 {ready} 条 · 未提交价格\n\n"
                + '\n'.join(rows))
    client = DingTalkClient(config)
    try:
        receipt = client.send_notice(userid, title, markdown, '')
    finally:
        client.close()
    result = dict(result, **receipt)
    with transaction(factory) as session:
        job = session.scalar(select(Job).where(Job.id == job_id, Job.owner == owner).with_for_update())
        if job is not None:
            job.payload = dict(job.payload, delivery=result)
    return result
