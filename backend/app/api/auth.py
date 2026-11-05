"""独立管理员会话：HttpOnly/SameSite cookie，不把凭据交给localStorage。"""
import base64
import hashlib
import hmac
import json
import time
from fastapi import HTTPException, Request

COOKIE = "lr_session"


def make_session(username, secret):
    payload = base64.urlsafe_b64encode(json.dumps({"u": username, "exp": int(time.time()) + 28800}).encode()).decode()
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def require_admin(request: Request):
    config = request.app.state.config
    token = request.cookies.get(COOKIE, "")
    try:
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(config.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        value = json.loads(base64.urlsafe_b64decode(payload))
        if value["exp"] <= time.time() or value["u"] != config.admin_username:
            raise ValueError()
        return value["u"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, "请先登录") from None


def password_matches(provided, expected):
    return bool(expected) and hmac.compare_digest(hashlib.sha256(provided.encode()).digest(), hashlib.sha256(expected.encode()).digest())
