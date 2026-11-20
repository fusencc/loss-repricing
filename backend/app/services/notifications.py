"""Persistent notices. Unknown sends never become retryable; only explicit failure can retry."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import time
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.models import Batch, Item, WorkerLease
from app.models.notifications import Notice
from app.adapters.dingtalk import DingTalkClient
from app.services.store import stamp, utcnow, BEIJING


NOTICE_ITEM_STATES = ('待处理', '待核验', '已核验', '本轮不调')


def _coord():
    from app.models.coordination import UserAccount
    from app.services.coordination import resolve_ownership
    return UserAccount, resolve_ownership


def _active(session, user_id, *, role=None):
    if user_id == 'admin':
        return role is None
    UserAccount,_ = _coord()
    user=session.get(UserAccount,user_id)
    return bool(user and user.active and (role is None or user.role == role))


def notice_visible(session, actor, notice):
    if actor.role == 'admin':
        return True
    if notice.recipient_id != actor.id or not notice.item_ids:
        return False
    from app.services.coordination import can_access
    keys = session.scalars(select(Item.key).where(Item.id.in_(notice.item_ids))).all()
    return len(keys) == len(notice.item_ids) and all(can_access(session, actor, key) for key in keys)


def _items(session,notice):
    return [session.get(Item,item_id) for item_id in notice.item_ids]


def _recipient_items(session, notice):
    role = 'manager' if notice.parent_id else None
    if not _active(session, notice.recipient_id, role=role):
        return []
    _,resolve=_coord()
    current=[]
    for item in _items(session,notice):
        if item is None:
            continue
        ownership=resolve(session,item.key)
        if notice.parent_id:
            # 主管降权或负责人停用时，网页权限与外部卡片的范围同步收紧。
            if (item.status not in {'待处理','待核验'} or not ownership
                    or not _active(session, ownership.owner_id)):
                continue
            expected=ownership.supervisor_id
        else:
            expected=ownership.owner_id if ownership and _active(session,ownership.owner_id) else 'admin'
        if expected == notice.recipient_id:
            current.append(item)
    return current


def _summary(items, escalation=False):
    counts=dict(Counter(i.status for i in items))
    exceptions=sum(i.status in {'待处理','待核验'} for i in items)
    return {'total':len(items),'exceptions':exceptions,'statuses':counts,
            'title':('异常超过24小时未确认' if escalation else '亏损提价处理结果'),
            'unassigned':False}


def notice_batch_mode(session, notice):
    return session.scalar(select(Batch.mode).where(Batch.id == notice.batch_id))


def _notice_category(item):
    if item.status == '已核验':
        return '已提价'
    if item.status == '本轮不调':
        return '本轮不调'
    return '待处理'


def _markdown_text(value):
    # 商品标识和原因属于来源文本，不能让其中的 Markdown 变成链接或格式指令。
    value=' '.join(str(value or '未提供').split())
    return ''.join('\\'+char if char in '\\[]()*_`<>#' else char for char in value)


def _card_text(summary, batch_mode, items):
    # 仅渲染发送前按当前收件人权限检查过的 items；明细不暴露其他销售员商品。
    # 时间是这些记录的最近处理时间（北京时间），不以重发时间冒充处理时间。
    processed_at=max(datetime.fromisoformat(item.updated_at) for item in items).astimezone(BEIJING)
    counts=Counter(_notice_category(item) for item in items)
    breakdown=' · '.join(f'{label} {counts[label]}' for label in ('待处理','已提价','本轮不调'))
    manual=[item for item in items if _notice_category(item)=='待处理']
    details=[]
    for index,item in enumerate(manual,1):
        key=item.key
        details.append(f"{index}. SKU：{_markdown_text(key.get('sku'))} · 站点：{_markdown_text(key.get('site'))} · 店铺：{_markdown_text(key.get('shop_id'))}\n\n"
                       f"   原因：{_markdown_text(item.reason)}")
    warning='演示批次（已提价项为模拟结果）\n\n' if batch_mode=='demo' else '文件对账（非真实提价）\n\n' if batch_mode=='file' else ''
    return (f"**{summary['title']}**\n\n{warning}"
            f"**{processed_at:%Y-%m-%d %H:%M:%S}** · 共处理**{len(items)}**条\n\n"
            f"{breakdown}\n\n待处理的明细：\n\n"
            + ('\n\n'.join(details) if details else '无')
            + '\n\n查询提价数据下载[http://8.133.252.166:50003](http://8.133.252.166:50003)')


def enqueue_batch_notices(factory, batch_id, *, existing_only=False):
    with factory.begin() as session:
        return write_batch_notices(session, batch_id, existing_only=existing_only)


def write_batch_notices(session, batch_id, *, existing_only=False):
    from app.models.coordination import Ownership
    from app.services.coordination import ownership_scopes

    # 通知汇总只用身份与状态，避免每次回查重复载入整批价格计算证据。
    items = session.execute(
        select(Item.id, Item.key, Item.status).where(Item.batch_id == batch_id)
    )
    items_with_scopes = [(item, ownership_scopes(item.key)) for item in items]
    scopes = sorted({scope for _, pair in items_with_scopes for scope in pair})
    owners = {}
    # 按当前批次的目标与店铺范围分段读取，避免每个商品重复查询归属。
    for start in range(0, len(scopes), 500):
        chunk = scopes[start:start + 500]
        owners.update((row.scope_key, row.owner_id) for row in session.scalars(
            select(Ownership).where(Ownership.scope_key.in_(chunk))
        ))
    active = {owner_id for owner_id in set(owners.values()) if _active(session, owner_id)}
    groups = defaultdict(list)
    unassigned = set()
    for item, (target_scope, shop_scope) in items_with_scopes:
        owner_id = owners.get(target_scope, owners.get(shop_scope))
        recipient = owner_id if owner_id in active else "admin"
        groups[recipient].append(item)
        if owner_id is None:
            unassigned.add(recipient)

    added = 0
    for recipient, items in groups.items():
        key = f"batch:{batch_id}:{recipient}"
        summary = _summary(items)
        summary["unassigned"] = recipient in unassigned
        existing=session.scalar(select(Notice).where(Notice.dedupe_key==key).with_for_update())
        if existing:
            # Refresh web business state only. Delivery, receipt time, ack and evidence are immutable here.
            existing.item_ids=[i.id for i in items]
            existing.summary=summary
            continue
        if existing_only:
            # 批次取数失败期间只同步原通知页，不新增投递。
            continue
        now=stamp(utcnow())
        try:
            with session.begin_nested():
                session.add(Notice(id=str(uuid4()),dedupe_key=key,recipient_id=recipient,
                    batch_id=batch_id,item_ids=[i.id for i in items],summary=summary,
                    delivery_state='pending',created_at=now,updated_at=now))
                session.flush()
            added+=1
        except IntegrityError:
            # 只吸收确实被另一事务插入的通知。其他约束错误须回滚本次批次汇总。
            if session.scalar(select(Notice.id).where(Notice.dedupe_key == key).with_for_update()) is None:
                raise
    return added


def _escalate(factory):
    _,resolve=_coord()
    now=utcnow()
    with factory.begin() as session:
        originals=session.scalars(select(Notice).where(Notice.parent_id.is_(None),Notice.ack_at.is_(None),
                                                       Notice.escalated_at.is_(None))).all()
        for notice in originals:
            if now-datetime.fromisoformat(notice.created_at)<timedelta(hours=24):
                continue
            groups=defaultdict(list)
            for item in _items(session,notice):
                if not item or item.status not in {'待处理','待核验'}:
                    continue
                ownership=resolve(session,item.key)
                # Never guess supervisor or reuse an ownership snapshot after transfer.
                if (ownership and ownership.owner_id==notice.recipient_id and ownership.supervisor_id
                        and _active(session, ownership.owner_id)
                        and _active(session, ownership.supervisor_id, role='manager')):
                    groups[ownership.supervisor_id].append(item)
            if not groups:
                continue
            for supervisor,items in groups.items():
                key=f'escalation:{notice.id}:{supervisor}'
                if not session.scalar(select(Notice.id).where(Notice.dedupe_key==key)):
                    try:
                        with session.begin_nested():
                            session.add(Notice(id=str(uuid4()),dedupe_key=key,recipient_id=supervisor,
                                batch_id=notice.batch_id,item_ids=[i.id for i in items],summary=_summary(items,True),
                                delivery_state='pending',parent_id=notice.id,created_at=stamp(now),updated_at=stamp(now)))
                            session.flush()
                    except IntegrityError:
                        pass
            notice.escalated_at=stamp(now)


def notification_status(factory,config):
    live=getattr(config,'notification_mode','demo')=='live'
    configured=all(getattr(config,k,'') for k in ('ding_client_id','ding_client_secret'))
    with factory() as session:
        counts=dict(Counter(session.scalars(select(Notice.delivery_state))))
    return {'mode':'live' if live else 'demo','enabled':bool(getattr(config,'ding_enabled',False)),
            'configured':bool(configured),'synthetic':not live,'counts':counts,
            'message':('演示模拟投递，不代表真实钉钉发送' if not live else
                       '机器人真实通知已启用' if configured and getattr(config,'ding_enabled',False) else
                       '真实通知需启用并完成应用配置')}


DISPATCH_LEASE = 'notification_dispatch'
LEASE_SECONDS = 120


def _acquire_dispatch(factory,owner):
    now=time.time()
    with factory.begin() as session:
        if session.get(WorkerLease,DISPATCH_LEASE) is None:
            try:
                with session.begin_nested():
                    session.add(WorkerLease(name=DISPATCH_LEASE,owner=owner,expires=0,heartbeat=stamp()))
                    session.flush()
            except IntegrityError:
                pass
        claimed=session.execute(update(WorkerLease).where(WorkerLease.name==DISPATCH_LEASE,
            WorkerLease.expires<=now).values(owner=owner,expires=now+LEASE_SECONDS,heartbeat=stamp()))
        return claimed.rowcount==1


def _renew_dispatch(factory,owner):
    now=time.time()
    with factory.begin() as session:
        changed=session.execute(update(WorkerLease).where(WorkerLease.name==DISPATCH_LEASE,
            WorkerLease.owner==owner,WorkerLease.expires>now).values(expires=now+LEASE_SECONDS,heartbeat=stamp()))
        return changed.rowcount==1


def dispatch_notices(factory,config):
    owner=str(uuid4())
    if not _acquire_dispatch(factory,owner):
        return dict(notification_status(factory,config),busy=True)
    try:
        return _dispatch_notices(factory,config,owner)
    finally:
        with factory.begin() as session:
            session.execute(update(WorkerLease).where(WorkerLease.name==DISPATCH_LEASE,
                WorkerLease.owner==owner).values(expires=0,heartbeat=stamp()))


def _dispatch_notices(factory,config,owner):
    # This transition is durable before a new HTTP send; prior process may have submitted.
    with factory.begin() as session:
        session.execute(update(Notice).where(Notice.delivery_state=='sending').values(
            delivery_state='unknown',last_error='发送过程中断，结果未知，禁止自动重发',updated_at=stamp(utcnow())))
    _escalate(factory)
    state=notification_status(factory,config)
    if state['mode']=='live' and (not state['enabled'] or not state['configured']):
        return state
    client=DingTalkClient(config) if state['mode']=='live' else None
    try:
        with factory() as session:
            ids=list(session.scalars(select(Notice.id).where(Notice.delivery_state.in_(['pending','accepted','delivered']))))
        for notice_id in ids:
            if not _renew_dispatch(factory,owner):
                break
            with factory.begin() as session:
                notice=session.get(Notice,notice_id)
                if notice.delivery_state in {'accepted','delivered'}:
                    if not client or notice.synthetic or not notice.task_id or not notice.recipient_userid:
                        continue
                    # Result endpoint retains evidence for only 24h. Keep acceptance, never resend.
                    if utcnow()-datetime.fromisoformat(notice.updated_at)>timedelta(hours=24):
                        continue
                    queries=[e for e in (notice.delivery_evidence or []) if e.get('operation')=='query']
                    delays=(15,60,300,900,3600)
                    if queries:
                        last=datetime.fromisoformat(queries[-1]['at'])
                        if (utcnow()-last).total_seconds() < delays[min(len(queries)-1,len(delays)-1)]:
                            continue
                    query=(notice.task_id,notice.recipient_userid)
                    send=None
                else:
                    current=_recipient_items(session,notice)
                    if not current:
                        notice.delivery_state='cancelled'
                        notice.last_error='已无当前有权查看的条目，通知已取消'
                        notice.updated_at=stamp()
                        continue
                    notice.item_ids=[i.id for i in current]
                    notice.summary=_summary(current,bool(notice.parent_id))
                    _,resolve=_coord()
                    unassigned=any(resolve(session,i.key) is None for i in current)
                    notice.summary=dict(notice.summary,unassigned=unassigned)
                    if not client:
                        notice.synthetic=True
                        notice.delivery_state='demo'
                        notice.last_error='演示模拟投递，未向钉钉发送'
                        notice.updated_at=stamp(utcnow())
                        continue
                    UserAccount,_=_coord()
                    user=session.get(UserAccount,notice.recipient_id) if notice.recipient_id!='admin' else None
                    userid=user.dingtalk_userid if user and user.active else None
                    if not userid:
                        notice.last_error='负责人未绑定钉钉userid，未发送'
                        continue
                    claimed=session.execute(update(Notice).where(Notice.id==notice_id,Notice.delivery_state=='pending').values(
                        delivery_state='sending',recipient_userid=userid,updated_at=stamp(utcnow())))
                    if claimed.rowcount != 1:
                        continue
                    notice.recipient_userid=userid
                    summary=notice.summary
                    text = _card_text(summary, notice_batch_mode(session, notice), current)
                    notice.delivery_evidence=list(notice.delivery_evidence or []) + [{
                        'operation':'send_intent','summary':dict(summary),'item_ids':list(notice.item_ids),
                        'title':summary['title'],'markdown':text,'at':stamp(utcnow())}]
                    send = (userid, summary['title'], text, '')
                    query=None
            # `sending` committed before crossing the external side-effect boundary.
            result=client.send_notice(*send) if send else client.query_notice(*query)
            with factory.begin() as session:
                notice=session.get(Notice,notice_id)
                notice.delivery_state=result['state']
                notice.delivery_evidence=list(notice.delivery_evidence or []) + [dict(result,operation='send' if send else 'query',at=stamp(utcnow()))]
                if result.get('task_id'):
                    notice.task_id=result['task_id']
                if result.get('request_id') and (send or not notice.request_id):
                    notice.request_id=result['request_id']
                notice.last_error=result.get('error')
                # Keep accepted timestamp fixed so polling cannot prolong the 24h retention window.
                if send:
                    notice.updated_at=stamp(utcnow())
    finally:
        if client:
            client.close()
    return notification_status(factory,config)

