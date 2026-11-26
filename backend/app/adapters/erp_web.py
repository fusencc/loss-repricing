"""ERP 网页服务的地址与可公开的错误证据。"""
from time import sleep

import httpx

from app.adapters.base import SourceUnavailable


class ErpBusinessFailure(SourceUnavailable):
    """仅保留数字业务码；未有合同的业务码不推断为无权限。"""

    def __init__(self, code):
        super().__init__(f'ERP接口业务失败（code={code}），需核实服务或权限')
        self.code = code


class ErpAccountRateLimited(ErpBusinessFailure):
    """500003 表示当前请求受账号限流，不自动重登。"""

    def __init__(self):
        super().__init__(500003)
        self.args = ('ERP账号操作频繁（500003），当前请求未取到数据',)


BLOCKED_LIST_RETRY_DELAYS = (16, 32)


def retry_erp_read(call, *, delays=(0, 0), wait=sleep):
    """Retry an idempotent read after a connection/read timeout or HTTP 5xx."""
    for attempt, delay in enumerate((0, *delays)):
        if delay:
            wait(delay)
        try:
            return call()
        except (httpx.ConnectTimeout, httpx.ReadTimeout):
            if attempt == len(delays):
                raise
        except httpx.HTTPStatusError as exc:
            if attempt == len(delays) or not 500 <= exc.response.status_code <= 599:
                raise


def safe_page_excerpt(response):
    """名单响应唯一允许存档的正文摘录：固定类型标签、业务码、计数；不复制键列表、消息或原行。"""
    def shape(obj, key):
        if not isinstance(obj, dict) or key not in obj:
            return 'absent'
        return {type(None): 'null', list: 'array', dict: 'object', int: 'integer',
                bool: 'boolean', str: 'string', float: 'number'}.get(type(obj[key]), 'other')

    evidence = {'http_status': response.status_code}
    try:
        body = response.json()
    except ValueError:
        return dict(evidence, body_shape='not_json')
    data = body.get('data') if isinstance(body, dict) else None
    code = body.get('code') if isinstance(body, dict) else None
    total = data.get('total') if isinstance(data, dict) else None
    rows = data.get('records') if isinstance(data, dict) else None
    return dict(evidence, body_shape=shape({'body': body}, 'body'), data_shape=shape(body, 'data'),
                code=code if type(code) is int else None, records_shape=shape(data, 'records'),
                rows=len(rows) if isinstance(rows, list) else None, total_shape=shape(data, 'total'),
                total=total if type(total) is int else None)


def error_evidence(error):
    status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
    kind = ('rate_limited' if isinstance(error, ErpAccountRateLimited) else
            'authentication_rejected' if status == 401 else
            'access_denied' if status == 403 else
            'business_failure' if isinstance(error, ErpBusinessFailure) else
            'read_failed')
    return {'error': _failure(error), 'error_kind': kind, 'http_status': status,
            'business_code': error.code if isinstance(error, ErpBusinessFailure) else None}

WEB_BASE = 'http://103.198.125.2:8077'


def _failure(error):
    # 只有明确HTTP拒绝才标记鉴权/授权问题；超时与业务失败不能推断权限不足。
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
        return 'ERP拒绝认证或授权，请核实该接口和数据范围权限'
    if isinstance(error, SourceUnavailable):
        return str(error)
    return 'ERP读取失败或返回格式无效，需核实服务和响应结构'
