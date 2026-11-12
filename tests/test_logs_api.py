"""仅使用隔离临时demo目录、数据库与合成事件，不访问真实接口。"""
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import os
import time
from uuid import NAMESPACE_OID, uuid5

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.api.auth import make_session
from app.api.logs import router
from app.config import Settings
from app.main import create_app
from app.models import Audit
from app.observability import project_event
from app.services import log_reader


@pytest.fixture
def client(tmp_path):
    config = Settings(database_url=f"sqlite:///{tmp_path / 'logs.db'}", runtime_dir=tmp_path,
                      admin_password="test-local-password", session_secret="test-session-secret-32-characters-long", _env_file=None)
    app = create_app(config)
    if not any(getattr(route, "path", None) == "/api/logs/status" for route in app.routes):
        app.include_router(router)
    with TestClient(app) as result:
        yield result


def admin(client):
    config = client.app.state.config
    client.cookies.set("lr_session", make_session(config.admin_username, config.session_secret))


def ident(value):
    return uuid5(NAMESPACE_OID, value).hex


def event(**extra):
    value = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": "ERROR", "role": "api",
             "event": "http.failed", "error_id": "err-001", **extra}
    value = {key: ident(item) if key.endswith("_id") else item for key, item in value.items()}
    # 与真正的中心事件目录对接；不替换project_event或向读取器注入假的脱敏函数。
    assert project_event(value) is not None
    return value


def write(client, rows, name="events.jsonl", role="api"):
    path = client.app.state.config.runtime_dir / "logs" / "demo" / role / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_logs_admin_only_and_status_has_no_runtime_path(client):
    assert client.get("/api/logs/status").status_code == 401
    assert client.get("/api/logs").status_code == 401
    assert client.get("/api/logs/errors/err-001").status_code == 401
    assert client.post("/api/logs/export", json={}).status_code == 401
    admin(client)
    status = client.get("/api/logs/status")
    assert status.status_code == 200
    assert str(client.app.state.config.runtime_dir) not in status.text


def test_bounded_page_cursor_filters_rotation_and_sanitization(client):
    admin(client)
    path = write(client, [event(error_id=f"err-{i}", password="never-return", raw_response="never-return") for i in range(3)])
    first = client.get("/api/logs", params={"limit": 1})
    assert first.status_code == 200
    body = first.json()
    assert len(body["items"]) == 1 and body["next_cursor"]
    assert "never-return" not in first.text
    cursor = body["next_cursor"]
    second = client.get("/api/logs", params={"limit": 1, "cursor": cursor})
    assert second.status_code == 200
    assert second.json()["items"] != body["items"]
    assert second.json()["window"] == body["window"]
    assert client.get("/api/logs", params={"cursor": cursor, "role": "worker"}).status_code == 422
    assert client.get("/api/logs", params={"cursor": cursor + "x"}).status_code == 422
    path.rename(path.with_name("events.jsonl.1"))
    expired = client.get("/api/logs", params={"cursor": cursor})
    assert expired.status_code == 409 and expired.json()["detail"] == "log_cursor_expired"


@pytest.mark.parametrize("params", [{"limit": 501}, {"role": "../api"}, {"level": "TRACE"},
    {"request_id": ".*"}, {"search": "password"}, {"start": "2026-10-01T00:00:00Z", "end": "2026-11-01T00:00:00Z"},
    {"start": "2026-11-01T00:00:00"}])
def test_strict_filters(client, params):
    admin(client)
    assert client.get("/api/logs", params=params).status_code == 422


def test_scan_limit_and_export_reject_incomplete(client, monkeypatch):
    admin(client)
    write(client, [event()])
    monkeypatch.setattr(log_reader, "MAX_SCAN_BYTES", 10)
    response = client.get("/api/logs").json()
    assert response["truncated"] and response["next_cursor"] is None
    assert "byte_limit" in response["truncation_reasons"]
    assert client.post("/api/logs/export", json={}).status_code == 409


def test_export_limit_and_persistent_audit(client, monkeypatch):
    admin(client)
    write(client, [event(password="not-exported")])
    exported = client.post("/api/logs/export", json={})
    assert exported.status_code == 200
    assert "not-exported" not in exported.text
    with client.app.state.factory() as session:
        audits = session.scalars(select(Audit).where(Audit.action == "logs.export")).all()
        assert len(audits) == 1 and audits[0].detail["count"] == len(exported.text.splitlines())
        assert audits[0].actor == "admin"
    monkeypatch.setattr(log_reader, "MAX_EXPORT_BYTES", 1)
    assert client.post("/api/logs/export", json={}).status_code == 413
    with client.app.state.factory() as session:
        assert len(session.scalars(select(Audit).where(Audit.action == "logs.export")).all()) == 1


def test_error_context_exact_specific_ids_not_process_or_unrelated_requests(client):
    admin(client)
    at = datetime.now(timezone.utc)
    write(client, [event(timestamp=at.isoformat(), execution_id="exec-1", request_id="request-shared", process_instance_id="process-1"),
                   event(error_id="err-002", execution_id="exec-1", request_id="another"),
                   event(error_id="err-unrelated", execution_id="exec-2", request_id="request-shared", process_instance_id="process-1"),
                   event(timestamp=(at - timedelta(hours=1)).isoformat(), error_id="err-old", execution_id="exec-1")])
    result = client.get(f"/api/logs/errors/{ident('err-001')}")
    assert result.status_code == 200
    body = result.json()
    assert body["correlation"] == {"execution_id": ident("exec-1")}
    assert {row["error_id"] for row in body["context"]["items"]} == {ident("err-001"), ident("err-002")}
    assert client.get(f"/api/logs/errors/{ident('not-found')}").status_code == 404


def test_symlink_files_and_parent_dirs_are_not_read(client, tmp_path):
    admin(client)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.jsonl").write_text(json.dumps(event(password="secret-outside")) + "\n")
    role = client.app.state.config.runtime_dir / "logs" / "demo" / "api"
    role.mkdir(parents=True, exist_ok=True)
    (role / "link.jsonl").symlink_to(outside / "secret.jsonl")
    result = client.get("/api/logs")
    assert result.status_code == 200 and result.json()["truncated"]
    assert "secret-outside" not in result.text
    (role / "link.jsonl").unlink()
    role.rename(role.with_name("saved-api"))
    role.symlink_to(outside, target_is_directory=True)
    result = client.get("/api/logs")
    assert result.status_code == 503 and str(tmp_path) not in result.text


def test_mode_isolation_and_rotated_file_reading(client):
    admin(client)
    write(client, [event(error_id="rotated")], name="events.jsonl.2", role="worker")
    live = client.app.state.config.runtime_dir / "logs" / "live" / "api"
    live.mkdir(parents=True)
    (live / "events.jsonl").write_text(json.dumps(event(error_id="live-only")) + "\n")
    response = client.get("/api/logs", params={"role": "worker"})
    assert response.status_code == 200
    assert [row["error_id"] for row in response.json()["items"]] == [ident("rotated")]
    assert ident("live-only") not in client.get("/api/logs").text


def test_missing_timestamp_partial_and_corrupt_lines_are_explicit(client):
    admin(client)
    row = event()
    row.pop("timestamp")
    path = write(client, [row])
    with path.open("ab") as stream:
        stream.write(b"not-json\n{\"unfinished\":")
    response = client.get("/api/logs").json()
    assert response["truncated"]
    assert {"invalid_event", "invalid_or_partial_line"} <= set(response["truncation_reasons"])
    assert response["corrupt_lines"] == 2
    assert response["partial_lines"] == 1
    assert response["scanned_lines"] >= 4
    assert response["skipped_lines"] >= 0
    assert client.post("/api/logs/export", json={}).status_code == 409


def test_expired_cursor_and_time_budget(client, monkeypatch):
    admin(client)
    write(client, [event(error_id=f"err-{i}") for i in range(3)])
    reader = log_reader.LogReader(client.app.state.config)
    cursor = client.get("/api/logs", params={"limit": 1}).json()["next_cursor"]
    payload = reader._decode(cursor)
    payload["expires"] = 1
    assert client.get("/api/logs", params={"cursor": reader._sign(payload)}).status_code == 409
    monkeypatch.setattr(log_reader, "MAX_SCAN_SECONDS", 0)
    response = client.get("/api/logs").json()
    assert response["truncated"] and "time_limit" in response["truncation_reasons"]
    assert client.post("/api/logs/export", json={}).status_code == 409


def test_ancestor_symlink_is_rejected_without_path_disclosure(client):
    admin(client)
    root = client.app.state.config.runtime_dir / "logs"
    root.rename(root.with_name("saved-logs"))
    root.symlink_to(root.with_name("saved-logs"), target_is_directory=True)
    response = client.get("/api/logs")
    assert response.status_code == 503
    assert str(root) not in response.text


@pytest.fixture
def reader(tmp_path):
    config = Settings(runtime_dir=tmp_path, session_secret="test-session-secret-32-characters-long", _env_file=None)
    return log_reader.LogReader(config)


def reader_file(reader, rows, name="events.jsonl", role="api"):
    path = reader.root / role / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
    return path


def cursor_payload():
    upper = datetime.now(timezone.utc)
    return {"filters": {}, "mode": "demo", "start": (upper - timedelta(hours=24)).isoformat(),
            "end": upper.isoformat(), "snapshot": "0" * 64, "offset": 0, "expires": int(time.time()) + 900}


@pytest.mark.parametrize("key,value", [("offset", -1), ("offset", "1"), ("offset", 1.5), ("offset", True),
    ("offset", {}), ("offset", 2**100), ("expires", True), ("expires", "999999999999"),
    ("snapshot", []), ("snapshot", "bad"), ("mode", []), ("mode", "other"),
    ("filters", []), ("filters", {"request_id": "bad"}), ("start", None), ("end", "invalid"), ("extra", "field")])
def test_cursor_fixed_schema_and_offset_validation(reader, key, value):
    payload = cursor_payload()
    payload[key] = value
    with pytest.raises(log_reader.LogReadError) as raised:
        reader.query(cursor=reader._sign(payload))
    assert raised.value.status == 422 and raised.value.code == "invalid_log_cursor"


def test_cursor_missing_schema_and_deep_json_are_invalid(reader):
    payload = cursor_payload()
    del payload["offset"]
    with pytest.raises(log_reader.LogReadError, match="invalid_log_cursor"):
        reader.query(cursor=reader._sign(payload))
    raw = b"[" * 2000 + b"0" + b"]" * 2000
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
    signature = hmac.new(reader.secret, b"log-cursor-v1:" + encoded, hashlib.sha256).hexdigest().encode()
    with pytest.raises(log_reader.LogReadError) as raised:
        reader.query(cursor=(encoded + b"." + signature).decode())
    assert raised.value.status == 422


def test_directory_budget_counts_nonmatching_entries(reader, monkeypatch):
    folder = reader.root / "api"
    folder.mkdir(parents=True)
    for index in range(10):
        (folder / f"ignored-{index}.txt").write_text("not a log")
    monkeypatch.setattr(log_reader, "MAX_DIRECTORY_ENTRIES", 3)
    response = reader.query()
    assert response["scanned_directory_entries"] == 3
    assert response["truncated"] and "directory_entry_limit" in response["truncation_reasons"]
    assert not response["items"] and response["next_cursor"] is None
    with pytest.raises(log_reader.LogReadError, match="log_export_incomplete"):
        reader.query(export=True)


def test_newest_files_first_and_budget_stops_before_opening_older(reader, monkeypatch):
    older = reader_file(reader, [event(error_id="old")], "old.jsonl", role="api")
    newer = reader_file(reader, [event(error_id="new")], "new.jsonl", role="worker")
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
    monkeypatch.setattr(log_reader, "MAX_SCAN_BYTES", newer.stat().st_size)
    real_open = os.open
    opened = []

    def tracked_open(path, *args, **kwargs):
        opened.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {tracked_open})
    monkeypatch.setattr(os, "open", tracked_open)
    response = reader.query()
    assert [row["error_id"] for row in response["items"]] == [ident("new")]
    assert response["scanned_bytes"] == newer.stat().st_size
    assert response["truncated"] and "byte_limit" in response["truncation_reasons"]
    assert "new.jsonl" in opened and "old.jsonl" not in opened


def test_oversized_file_tail_keeps_recent_whole_lines_and_rejects_export(reader, monkeypatch):
    old = event(timestamp=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat(), error_id="old")
    latest = event(error_id="latest")
    path = reader_file(reader, [old] * 30 + [latest])
    # mtime不是事件时间：旧mtime中的当前事件仍须读取，不能据此宣称完整跳过。
    os.utime(path, (1, 1))
    monkeypatch.setattr(log_reader, "MAX_SCAN_BYTES", len(json.dumps(latest).encode()) + 25)
    response = reader.query(start=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    assert [row["error_id"] for row in response["items"]] == [ident("latest")]
    assert response["scanned_bytes"] <= log_reader.MAX_SCAN_BYTES
    assert response["partial_lines"] == 1
    assert response["truncated"] and "file_tail_truncated" in response["truncation_reasons"]
    with pytest.raises(log_reader.LogReadError, match="log_export_incomplete"):
        reader.query(export=True)


def test_skipped_counts_include_unknown_events_window_and_exact_filters(reader):
    reader_file(reader, [event(error_id="keep", request_id="wanted"), event(error_id="filtered", request_id="other"),
                         event(timestamp=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat()),
                         {"event": "unknown.event"}])
    response = reader.query(filters={"request_id": ident("wanted")})
    assert response["scanned_lines"] == 4 and response["skipped_lines"] == 3
    assert response["corrupt_lines"] == response["partial_lines"] == 0
    assert not response["truncated"] and len(response["items"]) == 1


def test_first_directory_entry_disappearing_is_explicit_snapshot_change(reader, monkeypatch):
    (reader.root / "api").mkdir(parents=True)

    class VanishedEntry:
        name = "vanished.jsonl"

        def stat(self, **kwargs):
            raise FileNotFoundError()

    class Entries:
        def __enter__(self):
            return iter([VanishedEntry()])

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(os, "scandir", lambda fd: Entries())
    with pytest.raises(log_reader.LogReadError) as raised:
        reader.query()
    assert raised.value.status == 409 and raised.value.code == "log_snapshot_changed"
    with pytest.raises(log_reader.LogReadError) as raised:
        reader.query(cursor=reader._sign(cursor_payload()))
    assert raised.value.status == 409 and raised.value.code == "log_cursor_expired"


