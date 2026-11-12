"""Authenticated notification inbox. Delivery and business acknowledgment stay independent."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import case, func, select
from app.api.auth import read_principal, require_admin, require_user
from app.api.routes import item_view
from app.models import Item
from app.models.notifications import Notice
from app.services.notifications import (
    NOTICE_ITEM_STATES, dispatch_notices, notice_batch_mode, notice_visible, notification_status,
)
from app.services.store import stamp

router=APIRouter(prefix='/api')


def view(notice):
    # DingTalk recipient ID is internal routing metadata, not required by the inbox.
    return {column.name:getattr(notice,column.name) for column in Notice.__table__.columns
            if column.name != 'recipient_userid'}


@router.get('/notifications')
def inbox(request:Request,actor=Depends(require_user)):
    with request.app.state.factory() as session:
        query=select(Notice).order_by(Notice.created_at.desc())
        if actor.role!='admin':
            query=query.where(Notice.recipient_id==actor.id)
        return [view(n) for n in session.scalars(query) if notice_visible(session,actor,n)]


@router.get('/notifications/{notice_id}')
def detail(notice_id: str, request: Request, offset: int = Query(default=0, ge=0),
           limit: int = Query(default=25, ge=1, le=100)):
    with request.app.state.factory() as session:
        # 账号版本、归属和商品都从本次详情读取的会话核对。
        actor = read_principal(request, session)
        notice = session.get(Notice, notice_id)
        if notice is None or not notice_visible(session, actor, notice):
            raise HTTPException(404, '通知不存在或无权查看')

        # 发出时的摘要属于投递证据。打开时按当前关联条目读取业务结果。
        related = Item.id.in_(notice.item_ids)
        counts = dict(session.execute(
            select(Item.status, func.count()).where(related).group_by(Item.status)
        ).all())
        priority = case({status: index for index, status in enumerate(NOTICE_ITEM_STATES)},
                        value=Item.status, else_=len(NOTICE_ITEM_STATES))
        items = session.scalars(select(Item).where(related).order_by(priority, Item.id).offset(offset).limit(limit))
        return {
            'id': notice.id, 'batch_id': notice.batch_id, 'batch_mode': notice_batch_mode(session, notice),
            'title': notice.summary.get('title'), 'delivery_state': notice.delivery_state,
            'synthetic': notice.synthetic, 'ack_at': notice.ack_at, 'last_error': notice.last_error,
            'created_at': notice.created_at, 'statuses': counts, 'total': sum(counts.values()),
            'items': [item_view(session, item, actor) for item in items],
        }


@router.get('/notification-status',dependencies=[Depends(require_admin)])
def status(request:Request):
    return notification_status(request.app.state.factory,request.app.state.config)


@router.post('/notifications/dispatch',dependencies=[Depends(require_admin)])
def dispatch(request:Request):
    return dispatch_notices(request.app.state.factory,request.app.state.config)


@router.post('/notifications/{notice_id}/ack')
def acknowledge(notice_id:str,request:Request,actor=Depends(require_user)):
    with request.app.state.factory.begin() as session:
        notice=session.get(Notice,notice_id)
        if notice is None or not notice_visible(session,actor,notice):
            raise HTTPException(404,'通知不存在或无权查看')
        if notice.ack_at is None:
            notice.ack_at=stamp()
            notice.ack_by=actor.id
        return view(notice)


@router.post('/notifications/{notice_id}/retry',dependencies=[Depends(require_admin)])
def retry(notice_id:str,request:Request):
    with request.app.state.factory.begin() as session:
        notice=session.scalar(select(Notice).where(Notice.id==notice_id).with_for_update())
        if notice is None:
            raise HTTPException(404,'通知不存在')
        if notice.delivery_state!='failed':
            raise HTTPException(409,'仅明确失败通知可重试；已受理或结果未知禁止重发')
        notice.delivery_state='pending'
        notice.synthetic=False
        # Previous task/request IDs remain in delivery_evidence for audit.
        notice.task_id=None
        notice.request_id=None
        notice.recipient_userid=None
        notice.last_error=None
        notice.updated_at=stamp()
        return view(notice)
