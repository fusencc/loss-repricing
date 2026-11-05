"""外部接口传输日志脱敏：仅抑制当前认证/业务请求上下文。"""
from contextvars import ContextVar
import logging

private_request = ContextVar('loss_repricing_private_request', default=False)


class PrivateRequestFilter(logging.Filter):
    def filter(self, record):
        return not private_request.get()


for name in ('httpx', 'httpcore.connection', 'httpcore.http11', 'httpcore.http2', 'httpcore.proxy', 'httpcore.socks'):
    logging.getLogger(name).addFilter(PrivateRequestFilter())
