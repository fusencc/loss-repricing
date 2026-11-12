"""管理员日志读取：只读白名单事件；扫描预算与游标不参与任何业务执行。"""
from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import time

from app.observability import CATALOG, project_event, valid_id

ROLES = frozenset({"api", "worker", "capture", "desktop"})
LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.jsonl(?:\.[0-9]+)?\Z")
FILTER_KEYS = frozenset({"role", "level", "event", "error_id", "request_id", "job_id", "batch_id", "execution_id", "capture_id", "item_id", "operation_id"})
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_SCAN_SECONDS = 2.0
MAX_FILES = 4096
MAX_DIRECTORY_ENTRIES = 16384
MAX_LINE_BYTES = 64 * 1024
MAX_EXPORT_BYTES = 10 * 1024 * 1024


class LogReadError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def parse_time(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise LogReadError("invalid_log_time", 422) from None


def normalize_filters(values):
    if set(values) - FILTER_KEYS:
        raise LogReadError("invalid_log_filter", 422)
    result = {}
    for key, value in values.items():
        if value is None:
            continue
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise LogReadError("invalid_log_filter", 422)
        if key.endswith("_id") and not valid_id(value):
            raise LogReadError("invalid_log_filter", 422)
        if key == "event" and value not in CATALOG:
            raise LogReadError("invalid_log_filter", 422)
        if key == "role" and value not in ROLES or key == "level" and value not in LEVELS:
            raise LogReadError("invalid_log_filter", 422)
        result[key] = value
    return result


@contextmanager
def safe_directory(path: Path):
    """从文件系统根逐级 no-follow 打开，父目录软链接也不能跨出日志范围。"""
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        # 平台不能提供等价的防链接读取时，只关闭日志读取，不降级成不安全的路径打开。
        raise OSError("safe log open unavailable")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _fingerprint(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class LogReader:
    def __init__(self, config):
        self.root = Path(os.path.abspath(config.runtime_dir)) / "logs" / config.adapter_mode
        self.secret = config.session_secret.encode()
        self.mode = config.adapter_mode

    def _sign(self, value):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
        return (encoded + b"." + hmac.new(self.secret, b"log-cursor-v1:" + encoded, hashlib.sha256).hexdigest().encode()).decode()

    def _decode(self, cursor):
        try:
            if not isinstance(cursor, str) or len(cursor) > 8192:
                raise ValueError()
            encoded, signature = cursor.encode("ascii").rsplit(b".", 1)
            expected = hmac.new(self.secret, b"log-cursor-v1:" + encoded, hashlib.sha256).hexdigest().encode()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            value = json.loads(base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
            fields = {"filters", "mode", "start", "end", "snapshot", "offset", "expires"}
            if not isinstance(value, dict) or set(value) != fields:
                raise ValueError()
            if type(value["offset"]) is not int or not 0 <= value["offset"] <= MAX_SCAN_BYTES:
                raise ValueError()
            if type(value["expires"]) is not int or not 0 <= value["expires"] <= 2**63 - 1:
                raise ValueError()
            if value["mode"] not in {"demo", "file", "live"}:
                raise ValueError()
            if not isinstance(value["snapshot"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["snapshot"]):
                raise ValueError()
            if not isinstance(value["filters"], dict) or normalize_filters(value["filters"]) != value["filters"]:
                raise ValueError()
            if not isinstance(value["start"], str) or not isinstance(value["end"], str):
                raise ValueError()
            self.window(value["start"], value["end"])
            if value["expires"] < time.time():
                raise LogReadError("log_cursor_expired", 409)
            return value
        except LogReadError as exc:
            if exc.code == "log_cursor_expired":
                raise
            raise LogReadError("invalid_log_cursor", 422) from None
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise LogReadError("invalid_log_cursor", 422) from None

    def window(self, start=None, end=None):
        upper = parse_time(end) if end else datetime.now(timezone.utc)
        lower = parse_time(start) if start else upper - timedelta(hours=24)
        if lower >= upper or upper - lower > timedelta(days=14):
            raise LogReadError("invalid_log_window", 422)
        return lower, upper

    def _scan(self, filters, lower, upper):
        started = time.monotonic()
        files, rows, reasons = [], [], set()
        # skipped为合法但不匹配筛选/静态目录的行；corrupt为损坏/超长行；partial为不完整行或尾读边界片段。
        # 这些计数只描述本次实际检查部分，不估算未扫描前缀/文件中的行数。
        counts = {"scanned_lines": 0, "skipped_lines": 0, "corrupt_lines": 0, "partial_lines": 0}
        scanned, directory_entries = 0, 0
        root_opened = False

        def exhausted():
            if scanned >= MAX_SCAN_BYTES:
                reasons.add("byte_limit")
                return True
            if time.monotonic() - started > MAX_SCAN_SECONDS:
                reasons.add("time_limit")
                return True
            return False

        # 路径不来自客户端；枚举和读取共用已安全打开的目录描述符，防止父目录替换。
        try:
            with safe_directory(self.root) as root_fd, ExitStack() as directories:
                root_opened = True
                role_fds = {}
                enumeration_limited = False
                for role in sorted(ROLES):
                    if filters.get("role", role) != role:
                        continue
                    if enumeration_limited or exhausted():
                        break
                    try:
                        role_fd = os.open(role, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                    except FileNotFoundError:
                        continue
                    directories.callback(os.close, role_fd)
                    role_fds[role] = role_fd
                    with os.scandir(role_fd) as entries:
                        for entry in entries:
                            if directory_entries >= MAX_DIRECTORY_ENTRIES:
                                reasons.add("directory_entry_limit")
                                enumeration_limited = True
                                break
                            directory_entries += 1
                            if exhausted():
                                enumeration_limited = True
                                break
                            if not FILE_NAME.fullmatch(entry.name):
                                continue
                            if len(files) >= MAX_FILES:
                                reasons.add("file_limit")
                                enumeration_limited = True
                                break
                            # 第一个文件在stat前被轮转/删除也必须报快照变化，不能误报空日志。
                            info = entry.stat(follow_symlinks=False)
                            if not stat.S_ISREG(info.st_mode):
                                reasons.add("unsafe_file")
                                continue
                            files.append((role, entry.name, _fingerprint(info)))

                # mtime只用于优先读取最新文件，绝不作为事件时间或跳过旧文件的完整性证据。
                # 不假设系统时钟单调、轮转文件不可改或事件严格按时间排序。
                for role, name, identity in sorted(files, key=lambda file: (file[2][3], file[0], file[1]), reverse=True):
                    if exhausted():
                        break  # 预算用尽后不再打开其他文件。
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=role_fds[role])
                    with os.fdopen(fd, "rb") as stream:
                        if _fingerprint(os.fstat(stream.fileno())) != identity:
                            raise LogReadError("log_snapshot_changed", 409)
                        remaining = MAX_SCAN_BYTES - scanned
                        discard_line = False
                        if identity[2] > remaining:
                            # 大文件从预算允许的尾部读取，先判定边界并丢弃半行；少读的前缀明确计为截断。
                            # 因此缩小窗口不会被伪称为完整检索，导出仍拒绝此结果。
                            reasons.update({"byte_limit", "file_tail_truncated"})
                            stream.seek(identity[2] - remaining)
                            previous = stream.read(1)
                            scanned += len(previous)
                            discard_line = previous != b"\n"
                            if discard_line:
                                counts["scanned_lines"] += 1
                                counts["partial_lines"] += 1
                        while stream.tell() < identity[2]:
                            if exhausted():
                                break
                            line = stream.readline(min(MAX_LINE_BYTES + 1, MAX_SCAN_BYTES - scanned, identity[2] - stream.tell()))
                            if not line:
                                raise LogReadError("log_snapshot_changed", 409)
                            scanned += len(line)
                            if discard_line:
                                discard_line = not line.endswith(b"\n")
                                continue
                            counts["scanned_lines"] += 1
                            if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                                reasons.add("invalid_or_partial_line")
                                if len(line) > MAX_LINE_BYTES:
                                    counts["corrupt_lines"] += 1
                                else:
                                    counts["partial_lines"] += 1
                                discard_line = not line.endswith(b"\n")
                                continue
                            try:
                                data = json.loads(line)
                                if not isinstance(data, dict):
                                    raise ValueError()
                                projected = project_event(data)
                                if not projected:
                                    counts["skipped_lines"] += 1
                                    continue
                                timestamp = parse_time(projected.get("timestamp", projected.get("ts")))
                            except (ValueError, TypeError, RecursionError, OverflowError, LogReadError):
                                counts["corrupt_lines"] += 1
                                reasons.add("invalid_event")
                                continue
                            if not lower <= timestamp <= upper or any((role if key == "role" else projected.get(key)) != value for key, value in filters.items()):
                                counts["skipped_lines"] += 1
                                continue
                            rows.append((timestamp, role, name, stream.tell(), projected))
                        if _fingerprint(os.fstat(stream.fileno())) != identity:
                            raise LogReadError("log_snapshot_changed", 409)
        except FileNotFoundError:
            # 模式日志根目录尚未创建可以为空；根目录打开后发生的文件消失明确使快照失效。
            if root_opened:
                raise LogReadError("log_snapshot_changed", 409) from None
        except OSError:
            raise LogReadError("log_storage_unavailable", 503) from None
        # 固定时间窗之后的新事件不改变已有页；轮转、更换文件或窗内事件变化使游标失效。
        # 活动文件size/mtime不能直接作为跨页快照，否则查询自身的HTTP日志会使下一页过期。
        hasher = hashlib.sha256(json.dumps(sorted((role, name, identity[:2]) for role, name, identity in files), separators=(",", ":")).encode())
        rows.sort(key=lambda row: row[:4], reverse=True)
        for timestamp, role, name, position, projected in rows:
            if time.monotonic() - started > MAX_SCAN_SECONDS:
                reasons.add("time_limit")
                break
            hasher.update(json.dumps([timestamp.isoformat(), role, name, position, projected], sort_keys=True, separators=(",", ":")).encode())
        digest = hasher.hexdigest()
        return [row[4] for row in rows], digest, sorted(reasons), scanned, {**counts, "scanned_directory_entries": directory_entries}

    def query(self, *, filters=None, start=None, end=None, limit=100, cursor=None, export=False):
        filters = normalize_filters(filters or {})
        if type(limit) is not int or not 1 <= limit <= 500:
            raise LogReadError("invalid_log_limit", 422)
        offset, snapshot = 0, None
        lower, upper = self.window(start, end)
        if cursor:
            value = self._decode(cursor)
            if value.get("filters") != filters or value.get("mode") != self.mode:
                raise LogReadError("log_cursor_filter_mismatch", 422)
            if start and parse_time(start).isoformat() != value["start"] or end and parse_time(end).isoformat() != value["end"]:
                raise LogReadError("log_cursor_filter_mismatch", 422)
            lower, upper = parse_time(value["start"]), parse_time(value["end"])
            offset, snapshot = value["offset"], value["snapshot"]
        try:
            rows, digest, reasons, scanned, counts = self._scan(filters, lower, upper)
        except LogReadError as exc:
            if cursor and exc.code == "log_snapshot_changed":
                raise LogReadError("log_cursor_expired", 409) from None
            raise
        if snapshot is not None and digest != snapshot:
            raise LogReadError("log_cursor_expired", 409)
        if export:
            if reasons:
                raise LogReadError("log_export_incomplete", 409)
            output = bytearray()
            for row in rows:
                output.extend(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                if len(output) > MAX_EXPORT_BYTES:
                    raise LogReadError("log_export_too_large", 413)
            return bytes(output), len(rows), {"start": lower.isoformat(), "end": upper.isoformat(), "filters": filters}
        next_cursor = None
        if not reasons and offset + limit < len(rows):
            next_cursor = self._sign({"filters": filters, "mode": self.mode, "start": lower.isoformat(), "end": upper.isoformat(), "snapshot": digest, "offset": offset + limit, "expires": int(time.time()) + 900})
        return {"items": rows[offset:offset + limit], "next_cursor": next_cursor, "truncated": bool(reasons), "truncation_reasons": reasons, "scanned_bytes": scanned, **counts, "window": {"start": lower.isoformat(), "end": upper.isoformat()}}

    def error_context(self, error_id, *, start=None, end=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise LogReadError("invalid_log_limit", 422)
        normalize_filters({"error_id": error_id})
        found = self.query(filters={"error_id": error_id}, start=start, end=end, limit=1)
        if found["truncated"]:
            raise LogReadError("log_error_lookup_incomplete", 409)
        if not found["items"]:
            raise LogReadError("log_error_not_found", 404)
        error = found["items"][0]
        at = parse_time(error.get("timestamp", error.get("ts")))
        lower, upper = parse_time(found["window"]["start"]), parse_time(found["window"]["end"])
        # 只选最具体的一项显式关联，不以进程实例或所有关联ID的OR扩大到无关请求链。
        key = next((key for key in ("execution_id", "capture_id", "operation_id", "job_id", "batch_id", "request_id") if error.get(key)), "error_id")
        context = self.query(filters={key: error[key]}, start=max(lower, at - timedelta(minutes=5)).isoformat(), end=min(upper, at + timedelta(minutes=5)).isoformat(), limit=limit)
        return {"error": error, "context": context, "correlation": {key: error[key]}}
