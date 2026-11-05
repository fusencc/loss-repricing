"""每次使用当前归属和有效账号授权，排队快照不是权限依据。"""
from dataclasses import dataclass
from hashlib import sha256, scrypt
import secrets
import hmac
import json
from uuid import uuid4
from sqlalchemy import select
from app.models.coordination import UserAccount, Ownership, TargetControl
from app.domain.types import VariantKey
from app.services.store import stamp
from app.db import transaction

@dataclass(frozen=True)
class Principal:
    id: str
    username: str
    display_name: str
    role: str
    can_correct: bool=False


@dataclass(frozen=True)
class LockedAuthorization:
    """仅在持锁事务内使用，强引用保存 current read 的授权证据。

    MySQL REPEATABLE READ 的普通 SELECT 仍使用事务第一次读取的旧快照。
    因此锁完后不得再普通查询归属/账号，也不能只依赖 ORM 的弱引用缓存。
    """
    target_id: str
    owner: Ownership | None
    users: dict[str, UserAccount]

def hash_password(value):
    salt=secrets.token_bytes(16)
    return salt.hex()+':'+scrypt(value.encode(),salt=salt,n=16384,r=8,p=1).hex()

def verify_password(value,encoded):
    try:
        salt,digest=encoded.split(':')
        return hmac.compare_digest(scrypt(value.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex(),digest)
    except (ValueError,TypeError): return False

def principal(row):
    return Principal(row.id,row.username,row.display_name,row.role,row.can_correct)

def target_id(key):
    return VariantKey.model_validate(key).target_identity()

def scope_key(platform,shop_id,site,target=None):
    return sha256(json.dumps([platform,shop_id,site,target],ensure_ascii=False).encode()).hexdigest()

def resolve_ownership(session,key):
    k=VariantKey.model_validate(key)
    for target in [k.target_identity(),None]:
        row=session.scalar(select(Ownership).where(Ownership.scope_key==scope_key(k.platform,k.shop_id,k.site,target)))
        if row: return row
    return None

def active_owner(session,user_id,locked=None):
    if user_id=='admin': return True
    user=locked.users.get(user_id) if locked is not None else session.get(UserAccount,user_id)
    return bool(user and user.active)

def _authorization_owner(session,key,locked=None):
    if locked is None: return resolve_ownership(session,key)
    if locked.target_id!=target_id(key): raise ValueError('授权锁与目标不一致')
    return locked.owner

def can_access(session,actor,key,locked=None):
    if actor.role=='admin': return True
    row=_authorization_owner(session,key,locked)
    return bool(row and active_owner(session,row.owner_id,locked) and (row.owner_id==actor.id or (actor.role=='manager' and row.supervisor_id==actor.id)))

def get_control(session,key,create=False):
    ident=target_id(key)
    row=session.get(TargetControl,ident)
    if row is None and create:
        row=TargetControl(target_id=ident,key=dict(key) if isinstance(key,dict) else key.model_dump(),mode='auto',version=1,reason='',changed_by='system',updated_at=stamp(),baseline={},handback_pending=False)
        session.add(row); session.flush()
    return row

def lock_scope(session,platform,shop_id,site):
    """范围哨兵锁覆盖不存在的目标override，避免依赖MySQL隔离级别的gap lock。

    所有归属创建/修改和最终提交共用同一行；固定顺序control→scope/ownership→user→guard。
    Setting是已有表，不把不存在的override当作可锁定的普通行。
    """
    from app.models import Setting
    from sqlalchemy.exc import IntegrityError
    name='auth:'+scope_key(platform,shop_id,site)[:59]
    if session.get(Setting,name) is None:
        try:
            with session.begin_nested():
                session.add(Setting(key=name,value={}))
                session.flush()
        except IntegrityError: pass
    return session.scalar(select(Setting).where(Setting.key==name).with_for_update().execution_options(populate_existing=True))

def lock_authorization(session,key,user_ids=()):
    k=VariantKey.model_validate(key)
    lock_scope(session,k.platform,k.shop_id,k.site)
    keys=[scope_key(k.platform,k.shop_id,k.site,t) for t in [None,k.target_identity()]]
    rows=session.scalars(select(Ownership).where(Ownership.scope_key.in_(keys)).order_by(Ownership.id).with_for_update().execution_options(populate_existing=True)).all()
    ids=set(user_ids)
    for row in rows:ids.update([row.owner_id,row.supervisor_id])
    ids.discard(None);ids.discard('admin')
    users={}
    if ids:
        users={u.id:u for u in session.scalars(select(UserAccount).where(UserAccount.id.in_(sorted(ids))).order_by(UserAccount.id).with_for_update().execution_options(populate_existing=True)).all()}
    owner=next((r for r in rows if r.target_id==k.target_identity()),None) or next((r for r in rows if r.target_id is None),None)
    return LockedAuthorization(k.target_identity(),owner,users)

def authorization_snapshot(session,key,locked=None):
    owner=_authorization_owner(session,key,locked);control=get_control(session,key)
    return {'control_version':control.version if control else 0,'ownership_id':owner.id if owner else None,'ownership_version':owner.version if owner else None}

def automation_gate(session,key,authorization=None,locked=None):
    owner=_authorization_owner(session,key,locked)
    if not owner or not active_owner(session,owner.owner_id,locked): return False,'归属缺失或负责人已停用'
    if not owner.autonomous: return False,'归属未开启自动托管'
    control=get_control(session,key)
    if control and (control.mode!='auto' or control.handback_pending): return False,'人工接管或交回后等待重新取数'
    if authorization is not None and authorization!=authorization_snapshot(session,key,locked):return False,'管理状态或归属版本已变化，原自动方案失效'
    return True,''

def ensure_demo_ownership(factory,keys):
    with transaction(factory) as session:
        for key in keys:
            k=VariantKey.model_validate(key)
            sk=scope_key(k.platform,k.shop_id,k.site)
            if not session.scalar(select(Ownership).where(Ownership.scope_key==sk)):
                session.add(Ownership(id=str(uuid4()),scope_key=sk,platform=k.platform,shop_id=k.shop_id,site=k.site,owner_id='admin',autonomous=True,version=1))

def baseline_from_item(item):
    # 唯一价格依据是评估时存储的在线原始行；不能从建议目标价反推现价。
    rows=item.decision.get('trace',{}).get('online_rows',[])
    return {'online_rows':rows}
