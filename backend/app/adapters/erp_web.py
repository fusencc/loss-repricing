"""ERP 网页服务的地址与可公开的错误证据。"""
import httpx

from app.adapters.base import SourceUnavailable


class ErpBusinessFailure(SourceUnavailable):
    """仅保留数字业务码；未有合同的业务码不推断为无权限。"""

    def __init__(self, code):
        super().__init__(f'ERP接口业务失败（code={code}），需核实服务或权限')
        self.code = code


class ErpAccountRateLimited(ErpBusinessFailure):
    """2026-11-06已实测500003为账号限流；停止本轮，不自动重登或重试。"""

    def __init__(self):
        super().__init__(500003)
        self.args = ('ERP账号操作频繁，已停止本轮读取；恢复时间及正式限额尚待确认',)


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
