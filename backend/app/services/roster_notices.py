"""固定名单复用正式结果模板，按 ERP 采集范围中的姓名定向投递。"""
from sqlalchemy import select

from app.adapters.dingtalk import DingTalkClient
from app.db import transaction
from app.models import Batch, Execution, Item, Job
from app.services.store import enqueue, stamp
from app.services.notifications import _card_text, _markdown_text, _notice_category
from app.services.records import combine_pricing_process


SCOPE_STATUS = {
    'completed': '完成',
    'needs_attention': '有待核对',
    'blocked': '未完成',
    'failed': '失败',
}


def enqueue_roster_notices(factory, config, day, capture):
    if not (config.ding_roster_enabled or config.ding_enabled) or config.adapter_mode != 'live':
        return
    pricing = {row['salesperson']: row for row in capture.get('pricing_batches', [])}
    for name in capture['salespersons']:
        scopes = [
            {key: row[key] for key in ('department', 'status', 'ready_prices')}
            for row in capture['departments'] if row['salesperson'] == name
        ]
        if not scopes:
            continue
        payload = {'date': day.isoformat(), 'salesperson': name, 'scopes': scopes}
        if name in pricing:
            batch_id = pricing[name].get('batch_id')
            if batch_id:
                with factory() as session:
                    execution_states = session.scalars(
                        select(Execution.state).join(Item, Item.id == Execution.item_id)
                        .where(Item.batch_id == batch_id)
                    ).all()
                payload['pricing'] = {'verified': execution_states.count('verified'),
                                      'pending': sum(state in {'submitting', 'accepted', 'unknown', 'mismatch'}
                                                     for state in execution_states)}
            else:
                payload['pricing'] = {'blocked': True}
        enqueue(factory, 'erp_roster_notice', payload,
                f'erp-roster-notice:{day.isoformat()}:{name}')


def roster_notice_content(session, payload, created_at):
    # 仅取当日正式名单/库存补算中保存的本人记录，不借用网页默认归属或别人的批次。
    name, day = payload['salesperson'], payload['date']
    rows = session.execute(select(Item, Batch, Execution).join(Batch, Item.batch_id == Batch.id)
        .outerjoin(Execution, Execution.item_id == Item.id)
        .where(Batch.mode == 'live', Batch.business_date == day)
        .order_by(Item.updated_at.desc(), Item.id.desc()))
    chosen = [(item, execution) for item, batch, execution in rows
        if batch.run_key.startswith('erp-roster:') and batch.run_key.endswith(f':{name}:{day}')
        and (batch.snapshot.get('source_versions') or {}).get('erp_roster_salesperson') == name]
    # 与已部署记录页共用流程合并：补算成功后，旧准备/失败尝试不再计作待处理。
    projections = [{'id': item.id, 'business_date': day, 'mode': 'live', 'key': item.key,
                    'decision': item.decision, 'updated_at': item.updated_at, 'reason': item.reason,
                    'execution': {'state': execution.state,
                                  'submit_status': (execution.response or {}).get('status')} if execution else None}
                   for item, execution in chosen]
    retained = {row['id'] for row in combine_pricing_process(projections)}
    chosen = [(item, execution) for item, execution in chosen if item.id in retained]

    items = [item for item, _ in chosen]
    # 展示沿用用户“提交已受理即已提价”的口径，不改写真实执行状态或回读证据。
    categories = {item.id: ('已提价' if execution and
        (execution.state in {'accepted', 'verified'} or (execution.response or {}).get('status') == 'accepted')
        else _notice_category(item)) for item, execution in chosen}
    title = '亏损提价处理结果'
    text = _card_text({'title': title}, items, categories=categories, fallback_time=created_at)
    text += f'\n\n销售员：{_markdown_text(name)}'
    if not items:
        text += '\n\n暂无已保存的提价记录，未提交价格。'
    scopes = [f"- {_markdown_text(scope['department'])}：{SCOPE_STATUS.get(scope['status'], '状态待核对')}，"
              f"算价通过 {scope['ready_prices']} 条" for scope in payload['scopes']]
    return title, text + '\n\n采集状态：\n\n' + '\n'.join(scopes)



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
        if not (config.adapter_mode == 'live' and (config.ding_roster_enabled or config.ding_enabled)
                and config.ding_client_id and config.ding_client_secret):
            result = {'state': 'failed', 'error': '钉钉机器人未启用或应用配置不完整'}
        elif not userid.strip() or list(config.ding_roster_userids.values()).count(userid) != 1:
            result = {'state': 'failed', 'error': '销售员姓名未配置唯一钉钉 User ID'}
        else:
            result = {'state': 'sending', 'userid': userid, 'at': stamp()}
        if result['state'] == 'sending':
            title, markdown = roster_notice_content(session, payload, job.created_at)
            intent = {'title': title, 'markdown': markdown, 'at': result['at']}
            job.payload = dict(payload, delivery=result, send_intent=intent)
        else:
            job.payload = dict(payload, delivery=result)

    if result['state'] != 'sending':
        return result
    # 先持久化发送意图，再调用钉钉。租约恢复后只记录未知，不重发。
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
