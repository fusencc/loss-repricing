"""协作入口：范围鉴权、乐观版本和持久修正意图。"""
from dataclasses import asdict
from decimal import Decimal
from uuid import uuid4
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.api.auth import require_admin,require_user,COOKIE,make_session
from app.models import Batch,Item,Job,Audit
from app.models.coordination import UserAccount,Ownership,TargetControl,Correction
from app.services.coordination import *
from app.db import transaction
from app.services.store import utcnow, BEIJING, is_erp_reconciliation
router=APIRouter(prefix='/api')

def cols(row): return {c.name:getattr(row,c.name) for c in row.__table__.columns}
def user_view(row): return {k:v for k,v in cols(row).items() if k not in {'password_hash','session_version'}}
def can_view_item(session, actor, item):
    return actor.role == "admin" or (
        not is_erp_reconciliation(session.get(Batch, item.batch_id)) and can_access(session, actor, item.key)
    )


def check_item(session,item_id,actor, *, mode=None):
    item=session.get(Item,item_id)
    if not item or not can_view_item(session,actor,item): raise HTTPException(404,'记录不存在')
    batch = session.get(Batch, item.batch_id)
    if is_erp_reconciliation(batch):
        raise HTTPException(409, "文件对账批次仅供查阅")
    if mode is not None and batch.mode != mode:
        raise HTTPException(409, "修正来源批次模式不一致")
    return item

def item_collaboration(session,item,actor):
    own=resolve_ownership(session,item.key); control=get_control(session,item.key)
    ownership=cols(own) if own else None
    if ownership:
        for field in ['owner','supervisor']:
            uid=ownership.get(field+'_id'); user=session.get(UserAccount,uid) if uid and uid!='admin' else None
            ownership[field+'_name']='管理员' if uid=='admin' else user.display_name if user else None
    return {'control':cols(control) if control else {'target_id':target_id(item.key),'mode':'auto','version':0,'reason':'','handback_pending':False},
        'ownership':ownership,'capabilities':dict(takeover=True,pause=True,handback=True,propose_correction=True,approve_correction=actor.role in {'admin','manager'} or actor.can_correct)}

class Strict(BaseModel): model_config=ConfigDict(extra='forbid')
class UserCreate(Strict):
    username:str=Field(min_length=1,max_length=100)
    display_name:str=Field(min_length=1,max_length=100)
    password:str=Field(min_length=10,max_length=256)
    role:Literal['sales','manager']='sales'
    active:bool=True
    can_correct:bool=False
    dingtalk_userid:str|None=Field(default=None,max_length=128)
class UserPatch(Strict):
    display_name:str|None=Field(default=None,min_length=1,max_length=100)
    password:str|None=Field(default=None,min_length=10,max_length=256)
    role:Literal['sales','manager']|None=None
    active:bool|None=None
    can_correct:bool|None=None
    dingtalk_userid:str|None=Field(default=None,max_length=128)
@router.get('/users',dependencies=[Depends(require_admin)])
def users(request:Request):
    with request.app.state.factory() as s: return [user_view(u) for u in s.scalars(select(UserAccount))]
@router.post('/users',dependencies=[Depends(require_admin)])
def create_user(body:UserCreate,request:Request):
    if body.username in {'admin',request.app.state.config.admin_username}: raise HTTPException(409,'保留的管理员账号')
    data=body.model_dump(); password=data.pop('password')
    row=UserAccount(id=str(uuid4()),**data,password_hash=hash_password(password),session_version=1,created_at=stamp(),updated_at=stamp())
    try:
        with transaction(request.app.state.factory) as s: s.add(row); s.flush()
    except IntegrityError: raise HTTPException(409,'账号或钉钉绑定已存在') from None
    return user_view(row)
@router.patch('/users/{user_id}',dependencies=[Depends(require_admin)])
def patch_user(user_id:str,body:UserPatch,request:Request):
    try:
        with transaction(request.app.state.factory) as s:
            row=s.scalar(select(UserAccount).where(UserAccount.id==user_id).with_for_update().execution_options(populate_existing=True))
            if not row: raise HTTPException(404,'账号不存在')
            data=body.model_dump(exclude_unset=True)
            if any(data.get(k) is None for k in ['display_name','password','role','active','can_correct'] if k in data): raise HTTPException(422,'字段不可为空')
            if 'password' in data: row.password_hash=hash_password(data.pop('password'))
            for k,v in data.items(): setattr(row,k,v)
            row.session_version+=1;row.updated_at=stamp();s.flush()
    except IntegrityError: raise HTTPException(409,'钉钉绑定已存在') from None
    return user_view(row)

class OwnershipCreate(Strict):
    platform:str=Field(min_length=1,max_length=64)
    shop_id:str=Field(min_length=1,max_length=100)
    site:Literal['MY','PH','SG','TH','VN']
    target_id:str|None=Field(default=None,max_length=64)
    owner_id:str
    supervisor_id:str|None=None
    autonomous:bool=False
class OwnershipPatch(Strict):
    version:int=Field(ge=1)
    platform:str|None=Field(default=None,min_length=1,max_length=64)
    shop_id:str|None=Field(default=None,min_length=1,max_length=100)
    site:Literal['MY','PH','SG','TH','VN']|None=None
    target_id:str|None=None
    owner_id:str|None=None
    supervisor_id:str|None=None
    autonomous:bool|None=None

def validate_ownership(s,data):
    if not data.get('owner_id') or not active_owner(s,data['owner_id']): raise HTTPException(422,'负责人必须有效')
    sup=data.get('supervisor_id')
    if sup:
        u=s.get(UserAccount,sup)
        if not u or not u.active or u.role!='manager': raise HTTPException(422,'主管必须为有效主管账号')
@router.get('/ownerships',dependencies=[Depends(require_admin)])
def ownerships(request:Request):
    with request.app.state.factory() as s:return [cols(o) for o in s.scalars(select(Ownership))]
@router.get('/scopes')
def scopes(request:Request,actor=Depends(require_user)):
    with request.app.state.factory() as s:return [cols(o) for o in s.scalars(select(Ownership)) if actor.role=='admin' or o.owner_id==actor.id or actor.role=='manager' and o.supervisor_id==actor.id]
@router.post('/ownerships',dependencies=[Depends(require_admin)])
def add_ownership(body:OwnershipCreate,request:Request):
    data=body.model_dump()
    try:
        with transaction(request.app.state.factory) as s:
            lock_scope(s,data['platform'],data['shop_id'],data['site'])
            validate_ownership(s,data)
            row=Ownership(id=str(uuid4()),**data,scope_key=scope_key(data['platform'],data['shop_id'],data['site'],data['target_id']),version=1);s.add(row);s.flush()
    except IntegrityError:raise HTTPException(409,'归属范围已存在') from None
    return cols(row)
@router.patch('/ownerships/{own_id}',dependencies=[Depends(require_admin)])
def patch_ownership(own_id:str,body:OwnershipPatch,request:Request):
    try:
        with transaction(request.app.state.factory) as s:
            row=s.get(Ownership,own_id)
            if not row:raise HTTPException(404,'归属不存在')
            data={**cols(row),**body.model_dump(exclude_unset=True)}
            # Lock both old and new scope sentinels in lexical order before the ownership row.
            for platform,shop,site in sorted({(row.platform,row.shop_id,row.site),(data['platform'],data['shop_id'],data['site'])}):
                lock_scope(s,platform,shop,site)
            s.refresh(row,with_for_update=True)
            data={**cols(row),**body.model_dump(exclude_unset=True)}
            if data['version']!=row.version:raise HTTPException(409,'归属已变更，请刷新')
            validate_ownership(s,data)
            changes={k:v for k,v in data.items() if k not in {'id','version','scope_key'}}
            changes.update(scope_key=scope_key(data['platform'],data['shop_id'],data['site'],data['target_id']),version=row.version+1)
            changed=s.execute(update(Ownership).where(Ownership.id==own_id,Ownership.version==body.version).values(**changes))
            if changed.rowcount!=1:raise HTTPException(409,'归属已变更，请刷新')
            s.refresh(row)
    except IntegrityError:raise HTTPException(409,'归属范围已存在') from None
    return cols(row)

class ControlBody(Strict):
    action:Literal['takeover','pause','handback']
    reason:str=Field(min_length=1,max_length=500)
    version:int=Field(ge=0)
class CorrectionBody(Strict):
    desired_actual:str=Field(min_length=1,max_length=100)
    reason:str=Field(min_length=1,max_length=500)
    version:int=Field(ge=0)
class Approve(Strict): version:int=Field(ge=1)

def change_control(s,item,actor,version,reason,handback=False):
    control=get_control(s,item.key)
    actual=control.version if control else 0
    if version!=actual:raise HTTPException(409,'管理状态已变更，请刷新')
    if control is None:
        control=get_control(s,item.key,True)
        # First persisted version is 1 (the read-only default is version 0).
        control.mode='auto' if handback else 'manual';control.reason=reason;control.changed_by=actor.id;control.updated_at=stamp();control.handback_pending=handback
    else:
        result=s.execute(update(TargetControl).where(TargetControl.target_id==control.target_id,TargetControl.version==version).values(version=version+1,mode='auto' if handback else 'manual',reason=reason,changed_by=actor.id,updated_at=stamp(),handback_pending=handback))
        if result.rowcount!=1:raise HTTPException(409,'管理状态已变更，请刷新')
        s.refresh(control)
    return control
@router.post('/items/{item_id}/control')
def control(item_id:str,body:ControlBody,request:Request,actor=Depends(require_user)):
    try:
        with transaction(request.app.state.factory) as s:
            item=check_item(s,item_id,actor)
            row=change_control(s,item,actor,body.version,body.reason,body.action=='handback')
            s.add(Audit(id=str(uuid4()),action='control.'+body.action,actor=actor.id,detail={'target_id':row.target_id,'version':row.version},created_at=stamp()))
            s.flush()
    except IntegrityError:raise HTTPException(409,'管理状态已变更，请刷新') from None
    return {**cols(row),'message':'在途请求仍继续核验，执行锁保留'}
@router.get('/items/{item_id}/corrections')
def corrections(item_id:str,request:Request,actor=Depends(require_user)):
    with request.app.state.factory() as s:
        item=check_item(s,item_id,actor)
        return [cols(c) for c in s.scalars(select(Correction).where(Correction.target_id==target_id(item.key)))]
@router.post('/items/{item_id}/corrections')
def propose(item_id:str,body:CorrectionBody,request:Request,actor=Depends(require_user)):
    try: price=Decimal(body.desired_actual)
    except Exception:raise HTTPException(422,'售价必须为有限正十进制数') from None
    if not price.is_finite() or price<=0:raise HTTPException(422,'售价必须为有限正十进制数')
    with request.app.state.factory() as pre:
        source=check_item(pre,item_id,actor,mode=request.app.state.adapter.mode)
        source_key=VariantKey.model_validate(source.key)
    today=utcnow().astimezone(BEIJING).date()
    try:
        snapshot=request.app.state.adapter.collect(today)
        matches=[o for o in snapshot.online if o.key.target_identity()==source_key.target_identity()]
        now=utcnow()
        if snapshot.business_date!=today or len(matches)!=1 or matches[0].observed_at>now or (now-matches[0].observed_at).total_seconds()>300:
            raise ValueError()
        baseline={'online_rows':[matches[0].model_dump(mode='json')]}
    except Exception:raise HTTPException(409,'无法取得当日唯一且新鲜的在线价格依据') from None
    try:
        with transaction(request.app.state.factory) as s:
            item=check_item(s,item_id,actor,mode=request.app.state.adapter.mode)
            control=change_control(s,item,actor,body.version,body.reason)
            row=Correction(id=str(uuid4()),target_id=control.target_id,item_id=item.id,desired_actual=str(price),reason=body.reason,requested_by=actor.id,state='proposed',control_version=control.version,baseline=baseline,created_at=stamp(),updated_at=stamp(),result={})
            s.add(row);s.flush()
    except IntegrityError:raise HTTPException(409,'管理状态已变更，请刷新') from None
    return cols(row)
@router.post('/corrections/{correction_id}/approve')
def approve(correction_id:str,body:Approve,request:Request,actor=Depends(require_user)):
    with transaction(request.app.state.factory) as s:
        row=s.get(Correction,correction_id)
        if not row:raise HTTPException(404,'修正不存在')
        item=check_item(s,row.item_id,actor,mode=request.app.state.adapter.mode)
        if not (actor.role in {'admin','manager'} or actor.id==row.requested_by and actor.can_correct):raise HTTPException(403,'无修正批准权限')
        control=get_control(s,item.key)
        if not control or control.version!=body.version or row.control_version!=body.version:raise HTTPException(409,'修正依据版本已失效')
        if row.state=='queued':return cols(row)
        if row.state!='proposed':raise HTTPException(409,'修正状态不可批准')
        changed=s.execute(update(Correction).where(Correction.id==row.id,Correction.state=='proposed').values(state='queued',approved_by=actor.id,updated_at=stamp()))
        if changed.rowcount!=1:raise HTTPException(409,'修正已处理')
        job=Job(id=str(uuid4()),dedupe_key='correction:'+row.id,kind='correction',state='queued',payload={'correction_id':row.id},batch_id=item.batch_id,lease_until=0,created_at=stamp())
        s.add(job);s.flush();row.job_id=job.id;s.flush()
    return cols(row)

class DingLogin(Strict): code:str=Field(min_length=1,max_length=2048)
@router.get('/auth/dingtalk-config')
def ding_config(request:Request):
    c=request.app.state.config
    return {'enabled':bool(c.ding_enabled and c.ding_corp_id and c.ding_client_id),'corp_id':c.ding_corp_id,'client_id':c.ding_client_id}
@router.post('/auth/dingtalk')
def ding_login(body:DingLogin,request:Request,response:Response):
    c=request.app.state.config
    if not c.ding_enabled:raise HTTPException(409,'钉钉登录未配置')
    from app.adapters.dingtalk import DingTalkClient
    client=DingTalkClient(c)
    try:userid=client.user_from_code(body.code)
    except Exception:raise HTTPException(401,'钉钉登录验证失败') from None
    finally:client.close()
    if isinstance(userid,dict):userid=userid.get('userid')
    with request.app.state.factory() as s:
        user=s.scalar(select(UserAccount).where(UserAccount.dingtalk_userid==userid)) if userid else None
        if not user or not user.active:raise HTTPException(403,'钉钉账号未预先绑定有效本地用户')
        token=make_session(user.username,c.session_secret,user.id,user.session_version)
        result=asdict(principal(user))
    response.set_cookie(COOKIE,token,httponly=True,secure=c.secure_cookie,samesite='strict',max_age=28800,path='/')
    return result
