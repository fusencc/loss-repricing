"""HttpOnly签名会话；本地用户每次读取版本以即时撤销。"""
import base64
import hashlib
import hmac
import json
import time
from fastapi import HTTPException, Request
from app.models.coordination import UserAccount
from app.services.coordination import Principal, principal
COOKIE='lr_session'

def make_session(username,secret,user_id=None,version=None):
    value={'u':username,'exp':int(time.time())+28800}
    if user_id is not None: value.update(id=user_id,v=version)
    payload=base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    return payload+'.'+hmac.new(secret.encode(),payload.encode(),hashlib.sha256).hexdigest()

def require_user(request:Request):
    config=request.app.state.config
    try:
        payload,signature=request.cookies.get(COOKIE,'').rsplit('.',1)
        if not hmac.compare_digest(signature,hmac.new(config.session_secret.encode(),payload.encode(),hashlib.sha256).hexdigest()): raise ValueError()
        value=json.loads(base64.urlsafe_b64decode(payload))
        if value['exp']<=time.time(): raise ValueError()
        if value['u']==config.admin_username and value.get('id','admin')=='admin':
            return Principal('admin',config.admin_username,'管理员','admin',True)
        with request.app.state.factory() as session:
            row=session.get(UserAccount,value.get('id'))
            if not row or not row.active or row.username!=value['u'] or row.session_version!=value.get('v'): raise ValueError()
            return principal(row)
    except (ValueError,KeyError,TypeError): raise HTTPException(401,'请先登录') from None

def require_admin(request:Request):
    actor=require_user(request)
    if actor.role!='admin': raise HTTPException(403,'仅管理员可操作')
    return actor.username

def password_matches(provided,expected):
    return bool(expected) and hmac.compare_digest(hashlib.sha256(provided.encode()).digest(),hashlib.sha256(expected.encode()).digest())
