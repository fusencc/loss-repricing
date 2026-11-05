"""Persistent notices. Unknown sends never become retryable; only explicit failure can retry."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import time
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.models import Item, WorkerLease
from app.models.notifications import Notice
from app.adapters.dingtalk import DingTalkClient
from app.services.store import stamp, utcnow


def _coord():
    from app.models.coordination import UserAccount
    from app.services.coordination import resolve_ownership
    return UserAccount, resolve_ownership


def _active(session, user_id):
    if user_id == 'admin':
        return True
    UserAccount,_ = _coord()
    user=session.get(UserAccount,user_id)
    return bool(user and user.active)


def notice_visible(session, actor, notice):
    if actor.role == 'admin':
        return True
    if notice.recipient_id != actor.id:
        return False
    from app.services.coordination import can_access
    items=[session.get(Item,item_id) for item_id in notice.item_ids]
    return bool(items) and all(item and can_access(session,actor,item.key) for item in items)


def _items(session,notice):
    return [session.get(Item,item_id) for item_id in notice.item_ids]


def _recipient_items(session, notice):
    if not _active(session,notice.recipient_id):
        return []
    _,resolve=_coord()
    current=[]
    for item in _items(session,notice):
        if item is None:
            continue
        ownership=resolve(session,item.key)
        if notice.parent_id:
            if item.status not in {'待处理','待核验'}:
                continue
            expected=ownership.supervisor_id if ownership else None
        else:
            expected=ownership.owner_id if ownership and _active(session,ownership.owner_id) else 'admin'
        if expected == notice.recipient_id:
            current.append(item)
    return current


def _summary(items, escalation=False):
    counts=dict(Counter(i.status for i in items))
    exceptions=sum(i.status in {'待处理','待核验'} for i in items)
    return {'total':len(items),'exceptions':exceptions,'statuses':counts,
            'title':('异常超过24小时未确认' if escalation else '亏损提价批次处理结果'),
            'unassigned':False}


def enqueue_batch_notices(factory,batch_id):
    _,resolve=_coord()
    added=0
    with factory.begin() as session:
        groups=defaultdict(list)
        for item in session.scalars(select(Item).where(Item.batch_id==batch_id)):
            ownership=resolve(session,item.key)
            recipient=ownership.owner_id if ownership and _active(session,ownership.owner_id) else 'admin'
            groups[recipient].append(item)
        for recipient,items in groups.items():
            key=f'batch:{batch_id}:{recipient}'
            summary=_summary(items)
            summary['unassigned']=any(resolve(session,i.key) is None for i in items)
            existing=session.scalar(select(Notice).where(Notice.dedupe_key==key).with_for_update())
            if existing:
                # Refresh web business state only. Delivery, receipt time, ack and evidence are immutable here.
                existing.item_ids=[i.id for i in items]
                existing.summary=summary
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
                pass  # Unique outbox key arbitrates concurrent enqueue attempts.
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
                        and _active(session,ownership.supervisor_id)):
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
    configured=all(getattr(config,k,'') for k in ('ding_corp_id','ding_client_id','ding_client_secret','ding_agent_id','public_base_url'))
    with factory() as session:
        counts=dict(Counter(session.scalars(select(Notice.delivery_state))))
    return {'mode':'live' if live else 'demo','enabled':bool(getattr(config,'ding_enabled',False)),
            'configured':bool(configured),'synthetic':not live,'counts':counts,
            'message':'演示模拟投递，不代表真实钉钉发送' if not live else '真实通知需启用并完成应用配置'}


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
                    notice.delivery_evidence=list(notice.delivery_evidence or []) + [{
                        'operation':'send_intent','summary':dict(summary),'item_ids':list(notice.item_ids),
                        'at':stamp(utcnow())}]
                    text=f"本批共{summary.get('total',0)}条，其中异常{summary.get('exceptions',0)}条。请登录网页查看并确认。"
                    base=getattr(config,'public_base_url','').rstrip('/')
                    send=(userid,summary.get('title','亏损提价通知'),text,base+'/#notifications')
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

