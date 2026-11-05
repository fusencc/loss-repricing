"""Authenticated notification inbox. Delivery and business acknowledgment stay independent."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from app.api.auth import require_user, require_admin
from app.models.notifications import Notice
from app.services.notifications import notice_visible, notification_status, dispatch_notices
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
