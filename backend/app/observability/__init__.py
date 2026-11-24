"""安全运行日志：只记录约定事件与技术定位，不复制业务证据或外部异常文案。

本模块仅依赖标准库，也供独立 ERP 桌面脚本嵌入；日志故障不得改变提交、锁或重试。
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4
import inspect
import json
import logging
import math
import os
import re
import stat
import sys
import time

CATALOG = {
    'app.started': ('INFO', '进程初始化完成'),
    'app.start_failed': ('ERROR', '进程初始化失败'),
    'app.stopped': ('INFO', '进程已关闭'),
    'http.completed': ('INFO', '请求处理结束'),
    'http.rejected': ('WARNING', '请求被拒绝或校验未通过'),
    'http.failed': ('ERROR', '请求处理异常'),
    'worker.acquired': ('INFO', '后台进程已取得租约'),
    'worker.lease_lost': ('WARNING', '后台进程未持有租约'),
    'worker.heartbeat_failed': ('ERROR', '后台心跳失败，未改变执行状态'),
    'worker.heartbeat_recovered': ('INFO', '后台心跳恢复'),
    'worker.tick_failed': ('ERROR', '后台轮询失败，已有执行仍独立核验'),
    'worker.notice_dispatch_failed': ('ERROR', '通知投递轮询异常，取数和价格核验继续'),
    'worker.repeated': ('WARNING', '重复后台故障汇总'),
    'job.queued': ('INFO', '任务已持久入队'),
    'job.started': ('INFO', '后台任务开始'),
    'job.waiting': ('WARNING', '来源未就绪，按原规则等待重查'),
    'job.failed': ('ERROR', '后台任务失败，已提交记录仍独立核验'),
    'job.completed': ('INFO', '本次任务处理结束，不代表价格已生效'),
    'batch.started': ('INFO', '开始取数及评估'),
    'batch.saved': ('INFO', '批次与计算判定已保存'),
    'batch.completed': ('INFO', '批次处理结束，业务结果以持久记录为准'),
    'execution.blocked': ('INFO', '当前记录停止新提交，原因见业务记录'),
    'execution.roster_preflight_failed': ('WARNING', '固定名单商品提交前复核失败'),
    'execution.intent_saved': ('INFO', '执行意图及目标锁已持久保存'),
    'execution.submit_started': ('INFO', '开始外部提交，仅发送一次'),
    'execution.unknown': ('ERROR', '提交结果未知，保留锁且仅查询、不重传'),
    'execution.result_saved': ('INFO', '提交结果分类已保存，受理不等于生效'),
    'execution.lookup_failed': ('WARNING', '核验查询失败，保持原锁并稍后查询'),
    'execution.verified': ('INFO', '实际价核验通过，模拟属性另行标注'),
    'execution.rejected': ('WARNING', '已确认本次执行被拒绝'),
    'execution.mismatch': ('WARNING', '实际价超差，暂停本站点新提交'),
    'erp.started': ('INFO', 'ERP只读采集开始'),
    'erp.stage': ('INFO', 'ERP只读操作阶段进展'),
    'erp.completed': ('INFO', 'ERP读取结束，完整性另行判断'),
    'erp.failed': ('ERROR', 'ERP只读操作失败'),
    'capture.saved': ('INFO', '采集文件保存并回读一致，不代表业务验收'),
    'capture.cleaned': ('INFO', '采集订单清洗结果已保存'),
    'capture.clean_failed': ('ERROR', '采集订单清洗保存失败'),
    'capture.failed': ('ERROR', '采集文件保存或读取失败'),
    'logs.exported': ('INFO', '管理员已导出受限脱敏日志'),
    'dependency.failed': ('ERROR', '依赖组件报告错误，原始文案未保存'),
}
LEVELS = {'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50}
ROLES = {'api', 'worker', 'capture', 'desktop'}
ID_FIELDS = {'request_id', 'operation_id', 'job_id', 'batch_id', 'execution_id', 'capture_id', 'error_id', 'item_id'}
IDENTIFIER = re.compile(r'(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|erp-capture-[0-9]{8}T[0-9]{12}Z-[a-f0-9]{32})\Z')
FILE_NAME = re.compile(r'log-([0-9]+)-[a-f0-9]{32}\.jsonl(?:\.([0-9]+))?\Z')
_context = ContextVar('lr_log_context', default={})
_sink = ContextVar('lr_log_sink', default=None)
_default_sink = None
_registry = {}
_registry_lock = RLock()


def valid_id(value):
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def current_context():
    return dict(_context.get())


@contextmanager
def bind_context(**values):
    # 只接受本项目生成的技术编号，不让请求内容成为日志标签。
    clean = {k: v for k, v in values.items() if k in ID_FIELDS and valid_id(v)}
    token = _context.set({**_context.get(), **clean})
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def bind_sink(sink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def _symbol(value, limit=120):
    return isinstance(value, str) and len(value) <= limit and bool(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.<>]*', value))


def project_event(data):
    """写入和读取均投影白名单，历史损坏/额外字段不会经接口直接外传。"""
    if not isinstance(data, dict) or not isinstance(data.get('event'), str) or data['event'] not in CATALOG:
        return None
    name = data['event']
    level, message = CATALOG[name]
    result = {'event': name, 'level': level, 'message': message}
    value = data.get('timestamp')
    if isinstance(value, str) and len(value) <= 40:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                result['timestamp'] = parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    for key in ID_FIELDS:
        if valid_id(data.get(key)):
            result[key] = data[key]
    for key, options in {
        'role': ROLES, 'mode': {'demo', 'file', 'live'},
        'source': {'erp', 'demo', 'file', 'live'},
        'method': {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'OTHER'},
        'kind': {'run', 'reconcile', 'correction', 'other'},
        'state': {'queued', 'running', 'done', 'failed', 'accepted', 'rejected', 'unknown', 'submitting', 'verified', 'mismatch', 'not_submitted'},
        'stage': {'auth', 'collect', 'export', 'download', 'parse', 'save', 'read', 'rates', 'pool', 'finance', 'poll', 'start', 'complete'},
        'error_code': {'rate_limited', 'source_unavailable', 'invalid_data', 'storage_failed', '500003', 'http_error', 'incomplete', 'unknown'},
    }.items():
        if isinstance(data.get(key), str) and data[key] in options:
            result[key] = data[key]
    for key in ('synthetic', 'complete', 'limited'):
        if type(data.get(key)) is bool:
            result[key] = data[key]
    for key in ('pid', 'count', 'total', 'http_status', 'line', 'suppressed'):
        value = data.get(key)
        if type(value) is int and 0 <= value <= 10**12:
            result[key] = value
    value = data.get('duration_ms')
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**12:
        result['duration_ms'] = round(value, 3)
    if valid_id(data.get('instance')):
        result['instance'] = data['instance']
    for key in ('function', 'exception_type'):
        if _symbol(data.get(key)):
            result[key] = data[key]
    value = data.get('file')
    if isinstance(value, str) and len(value) <= 160 and re.fullmatch(r'[a-zA-Z0-9_/.-]+\.py', value) and not value.startswith('/') and '..' not in value:
        result['file'] = value
    value = data.get('route')
    if isinstance(value, str) and len(value) <= 180 and re.fullmatch(r'/[a-zA-Z0-9_/{}/.:-]*|unmatched', value):
        result['route'] = value
    stack = data.get('stack')
    if isinstance(stack, list):
        frames = []
        for frame in stack[-24:]:
            if not isinstance(frame, dict):
                continue
            safe = project_event({'event': name, **{k: frame[k] for k in ('file', 'function', 'line') if k in frame}})
            frames.append({k: safe[k] for k in ('file', 'function', 'line') if k in safe})
        result['stack'] = frames
    return result


def _location(frame):
    filename = Path(frame.f_code.co_filename)
    # 只留相对模块/文件名，不暴露用户目录、源码正文或局部变量。
    parts = filename.parts
    if 'app' in parts:
        filename = Path(*parts[parts.index('app'):])
    else:
        filename = Path(filename.name)
    return {'file': filename.as_posix(), 'function': frame.f_code.co_name, 'line': frame.f_lineno}


def _safe_mkdir(path):
    # runtime可在不同平台上配置；每一级都拒绝符号链接，文件另用O_NOFOLLOW。
    path = Path(os.path.abspath(path))
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise OSError('unsafe log directory')
        parent.mkdir(exist_ok=True, mode=0o700)
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise OSError('unsafe log directory')
    return path


def _alive(pid):
    if os.name == 'nt':
        # Windows的os.kill(pid,0)可能终止目标，不能用POSIX探活方式。
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


class Sink:
    def __init__(self, config, role):
        self.root = Path(config.runtime_dir) / 'logs' / config.adapter_mode
        self.role, self.mode = role, config.adapter_mode
        self.level = getattr(config, 'log_level', 'INFO')
        self.max_bytes = getattr(config, 'log_max_bytes', 20 * 1024 * 1024)
        self.retention_days = getattr(config, 'log_retention_days', 14)
        self.total_bytes = getattr(config, 'log_total_bytes', 1024**3)
        self.instance, self.pid = uuid4().hex, os.getpid()
        self.path = self.root / role / f'log-{self.pid}-{self.instance}.jsonl'
        self.lock = RLock()
        self.last_write = None
        self.write_ok = None
        self.cleanup_ok = None
        self.last_cleanup = 0
        self.rotation = 0
        self.last_warning = -1e10

    def _fallback(self, *, write_failure=True):
        if write_failure:
            self.write_ok = False
        if time.monotonic() - self.last_warning >= 60:
            self.last_warning = time.monotonic()
            try:
                sys.stderr.write('运行日志保存异常；请检查日志目录权限及磁盘空间，业务状态未被日志系统修改。\n')
                sys.stderr.flush()
            except Exception:
                pass

    def write(self, data):
        try:
            if LEVELS[data['level']] < LEVELS[self.level]:
                return
            encoded = (json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')
            with self.lock:
                _safe_mkdir(self.path.parent)
                if self.path.is_symlink():
                    raise OSError('unsafe log file')
                if self.path.exists() and self.path.stat().st_size + len(encoded) > self.max_bytes:
                    self.rotation += 1
                    target = self.path.with_name(self.path.name + '.' + str(self.rotation))
                    if target.exists() or target.is_symlink():
                        raise OSError('unsafe rotation target')
                    self.path.rename(target)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), 0o600)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError('unsafe log file')
                    remaining = memoryview(encoded)
                    while remaining:
                        count = os.write(fd, remaining)
                        if count <= 0:
                            raise OSError('short log write')
                        remaining = remaining[count:]
                finally:
                    os.close(fd)
                self.last_write, self.write_ok = data['timestamp'], True
                if time.monotonic() - self.last_cleanup >= 60:
                    self.last_cleanup = time.monotonic()
                    self.cleanup()
            # 摘要只使用静态目录中的文字和本地生成的技术编号。
            try:
                suffix = ' 排查编号：' + data['error_id'] if data.get('error_id') else ''
                sys.stderr.write(f"{data['level']} {data['event']} {data['message']}{suffix}\n")
            except Exception:
                pass
        except Exception:
            self._fallback()

    def cleanup(self):
        try:
            if any(path.is_symlink() for path in (self.root, *self.root.parents)):
                raise OSError('unsafe cleanup directory')
            files, total, inspected = [], 0, 0
            started = time.monotonic()
            cutoff = time.time() - self.retention_days * 86400
            for role in sorted(ROLES):
                folder = self.root / role
                if not folder.exists() or folder.is_symlink():
                    continue
                for path in folder.iterdir():
                    inspected += 1
                    if inspected > 4096 or time.monotonic() - started > 1:
                        raise OSError('cleanup budget exhausted')
                    match = FILE_NAME.fullmatch(path.name)
                    if not match or path.is_symlink():
                        continue
                    try:
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        total += info.st_size
                        # 活动主文件始终保留；已封口轮转文件可以按保留策略清理。
                        active = match.group(2) is None and _alive(int(match.group(1)))
                        if not active:
                            files.append((info.st_mtime, path, info.st_size))
                    except FileNotFoundError:
                        continue
            for modified, path, size in sorted(files):
                if modified >= cutoff and total <= self.total_bytes:
                    continue
                try:
                    path.unlink()
                    total -= size
                except FileNotFoundError:
                    pass
            self.cleanup_ok = total <= self.total_bytes
            if not self.cleanup_ok:
                self._fallback(write_failure=False)
        except Exception:
            self.cleanup_ok = False
            self._fallback(write_failure=False)

    def status(self):
        total, count, observed, truncated = 0, 0, {}, False
        started = time.monotonic()
        try:
            if any(path.is_symlink() for path in (self.root, *self.root.parents)):
                raise OSError('unsafe log directory')
            for role in sorted(ROLES):
                folder = self.root / role
                if folder.is_symlink() or not folder.exists():
                    continue
                latest = None
                for path in folder.iterdir():
                    if count >= 4096 or time.monotonic() - started > 1:
                        truncated = True
                        break
                    if not FILE_NAME.fullmatch(path.name) or path.is_symlink():
                        continue
                    try:
                        info = path.lstat()
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    count += 1
                    total += info.st_size
                    latest = info.st_mtime if latest is None else max(latest, info.st_mtime)
                if latest is not None:
                    # 文件更新时间只是观察值，不能证明另一进程仍健康。
                    observed[role] = {'last_file_write': datetime.fromtimestamp(latest, timezone.utc).isoformat()}
                if truncated:
                    break
        except OSError:
            truncated = True
        return {'role': self.role, 'mode': self.mode, 'level': self.level,
                'write_ok': self.write_ok, 'last_write': self.last_write,
                'cleanup_ok': self.cleanup_ok, 'max_file_bytes': self.max_bytes,
                'retention_days': self.retention_days, 'total_budget_bytes': self.total_bytes,
                'observed_total_bytes': total, 'observed_file_count': count,
                'usage_truncated': truncated, 'observed_roles': observed}


class SafeThirdPartyHandler(logging.Handler):
    """不转存第三方msg/args；有异常时仅提取同一安全技术栈，并限制重复输出。"""
    def __init__(self):
        super().__init__()
        self.last_error = -1e10
        self.suppressed = 0

    def emit(self, record):
        if record.levelno >= logging.ERROR:
            try:
                if time.monotonic() - self.last_error < 60:
                    self.suppressed += 1
                    return
                self.last_error = time.monotonic()
                exc = record.exc_info[1] if isinstance(record.exc_info, tuple) and len(record.exc_info) > 1 else None
                event('dependency.failed', exception=exc if isinstance(exc, BaseException) else None,
                      suppressed=self.suppressed)
                self.suppressed = 0
            except Exception:
                pass


def configure_logging(config, role='api'):
    global _default_sink
    if role not in ROLES:
        raise ValueError('invalid logging role')
    key = (os.getpid(), str(Path(config.runtime_dir).absolute()), config.adapter_mode, role,
           getattr(config, 'log_level', 'INFO'), getattr(config, 'log_max_bytes', 20 * 1024 * 1024),
           getattr(config, 'log_retention_days', 14), getattr(config, 'log_total_bytes', 1024**3))
    with _registry_lock:
        if key not in _registry:
            _registry[key] = Sink(config, role)
        _default_sink = _registry[key]
        # 不修改业务HTTP行为；仅封闭默认日志旁路。根日志也不接收原始异常文案。
        handler = SafeThirdPartyHandler()
        for name in ('', 'uvicorn', 'uvicorn.error', 'uvicorn.access', 'httpx', 'httpcore', 'sqlalchemy.engine', 'sqlalchemy.pool'):
            logger = logging.getLogger(name)
            logger.handlers[:] = [handler]
            logger.propagate = False
        logging.getLogger('uvicorn.access').disabled = True
    return _default_sink


def get_sink(config=None, role=None):
    if config is not None:
        key_root = Path(config.runtime_dir).absolute()
        found = _sink.get() or _default_sink
        if found and found.root.absolute() == key_root / 'logs' / config.adapter_mode and (role is None or found.role == role):
            return found
        return configure_logging(config, role or 'api')
    return _sink.get() or _default_sink


def event(name, exception=None, **fields):
    """事件错误编号即使保存失败也可返回，但绝不把日志写失败作为业务异常抛出。"""
    try:
        if name not in CATALOG:
            return None
        data = {**_context.get(), **fields, 'event': name,
                'timestamp': datetime.now(timezone.utc).isoformat()}
        error_id = None
        if exception is not None:
            error_id = uuid4().hex
            data.update(error_id=error_id, exception_type=type(exception).__name__)
            stack, tb = [], exception.__traceback__
            while tb is not None:
                location = _location(tb.tb_frame)
                location['line'] = tb.tb_lineno
                stack.append(location)
                tb = tb.tb_next
            data['stack'] = stack[-24:]
        frame = inspect.currentframe()
        try:
            if frame and frame.f_back:
                data.update(_location(frame.f_back))
        finally:
            del frame
        sink = get_sink()
        if sink is not None:
            data.update(role=sink.role, mode=sink.mode, pid=sink.pid, instance=sink.instance)
            sink.write(project_event(data))
        return error_id
    except Exception:
        return None
