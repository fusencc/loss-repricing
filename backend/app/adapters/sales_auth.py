"""销售系统自动取证：只调用已实测登录接口，不包含改价或业务请求。

依据：公司YApi basic-server/21040与2026-11-05本账号实测。文档标JSON，
JSON实测400，旧客户端application/x-www-form-urlencoded实测200并返回Token。
expires_in是从发证起的秒数。刷新接口尚无合同，使用同一登录接口重新取证；
refresh_token不持久化、不输出、不猜测刷新URL。scope=app不代表调价权限。
"""
from threading import Lock
import time

import httpx
from app.adapters.base import SourceUnavailable
from app.adapters.transport_privacy import private_request
from app.config import Settings

LOGIN_PATH = '/Api/api-auth/oauth/user/token'
ORIGIN = 'http://121.46.237.218:8071'
TIMEOUT = httpx.Timeout(30, connect=5)


class SalesAuth:
    def __init__(self, settings: Settings, client: httpx.Client, *, clock=time.monotonic):
        self.origin = settings.vendor_api_url.rstrip('/')
        self.configured = bool(self.origin == ORIGIN and settings.vendor_username
                               and settings.vendor_password.get_secret_value())
        self._username = settings.vendor_username
        self._password = settings.vendor_password
        self._client = client
        self._clock = clock
        self._lock = Lock()
        self._token = None
        self._renew_at = 0
        self._expires_at = 0
        self._lifetime = None
        self._scope = None
        self._has_refresh = False

    def get_token(self, *, force_login=False):
        with self._lock:
            if not force_login and self._token and self._clock() < self._renew_at:
                return self._token
            if not self.configured:
                raise SourceUnavailable('销售系统独立账号密码或已确认服务地址尚未配置')
            # 提前续期，只复用仍在有效范围内的认证结果；登录失败不能回退过期Token。
            self._token = None
            started = self._clock()
            context = private_request.set(True)
            try:
                response = self._client.post(self.origin + LOGIN_PATH,
                    headers={'client_id': 'webApp', 'client_secret': 'webApp'},
                    data={'username': self._username, 'password': self._password.get_secret_value()},
                    timeout=TIMEOUT, follow_redirects=False)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError()
                token = payload.get('access_token')
                if not isinstance(token, str) or not token.strip():
                    raise ValueError()
                duration = payload.get('expires_in')
                if isinstance(duration, bool) or not isinstance(duration, (int, str)):
                    raise ValueError()
                duration = int(duration)
                if duration <= 0 or str(payload.get('token_type', 'bearer')).lower() != 'bearer':
                    raise ValueError()
                self._expires_at = started + duration
                # 最多提前60秒；短寿命令牌预留10%，避免刚获取就重新登录。
                self._renew_at = self._expires_at - min(60, duration / 10)
                if self._clock() >= self._renew_at:
                    raise ValueError()
                self._token = token.strip()
                self._lifetime = duration
                self._scope = payload.get('scope') if isinstance(payload.get('scope'), str) else None
                self._has_refresh = bool(payload.get('refresh_token'))
                return self._token
            except (httpx.HTTPError, ValueError, TypeError, ArithmeticError):
                raise SourceUnavailable('销售系统登录失败或凭证有效期无效；未执行业务请求') from None
            finally:
                private_request.reset(context)

    def summary(self):
        return {'authenticated': bool(self._token and self._clock() < self._expires_at),
                'expires_in_seconds': self._lifetime, 'scope': self._scope,
                'refresh_token_present': self._has_refresh,
                'renewal_method': 'relogin_before_expiry', 'pricing_permission_verified': False}
