"""仅使用临时目录和合成异常，验证日志不成为业务数据外传或重传路径。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import json
import logging
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.observability import (Sink, bind_context, bind_sink, configure_logging, current_context,
                               event, project_event)
from app.services.store import enqueue
from app.models import Job
from app.worker import Worker


def config(tmp_path, **kwargs):
    return Settings(runtime_dir=tmp_path, database_url=f'sqlite:///{tmp_path}/test.db',
                    admin_password='test-only-password', session_secret='s' * 40,
                    coordination_required=False, _env_file=None, **kwargs)


def records(sink):
    return [json.loads(line) for path in sink.root.rglob('*.jsonl*')
            if path.is_file() for line in path.read_text().splitlines()]


def test_structured_safe_stack_and_reinitialization(tmp_path, capsys):
    cfg = config(tmp_path)
    sink = configure_logging(cfg)
    assert configure_logging(cfg) is sink
    assert len(logging.getLogger('uvicorn.error').handlers) == 1
    request_id = uuid4().hex
    secret = 'password=do-not-save https://private.test/file?token=secret-abc'
    with bind_sink(sink), bind_context(request_id=request_id, password=secret):
        try:
            raise ValueError(secret)
        except ValueError as exc:
            error_id = event('http.failed', exception=exc, password=secret,
                             response={'token': secret}, stage='secret-free-form-text')
    row = records(sink)[0]
    assert row['request_id'] == request_id and row['error_id'] == error_id
    assert row['exception_type'] == 'ValueError'
    assert row['stack'][-1]['function'] == 'test_structured_safe_stack_and_reinitialization'
    assert row['stack'][-1]['line'] > 0
    assert secret not in json.dumps(row)
    assert secret not in capsys.readouterr().err
    assert current_context() == {}


def test_projection_drops_unsafe_fields_and_invalid_events():
    assert project_event({'event': []}) is None
    assert project_event({'event': 'invented'}) is None
    row = project_event({'event': 'http.failed', 'message': 'password', 'stack': [
        {'file': '/private/secret.py', 'function': 'handle', 'line': 4, 'locals': 'secret'}],
        'route': '/api/test?token=private', 'request_id': '../secret', 'duration_ms': float('nan')})
    assert row['message'] != 'password'
    assert 'file' not in row['stack'][0] and 'locals' not in row['stack'][0]
    assert 'route' not in row and 'request_id' not in row and 'duration_ms' not in row


def test_debug_and_third_party_messages_do_not_expose_secrets(tmp_path, capsys):
    sink = configure_logging(config(tmp_path, log_level='DEBUG'))
    with bind_sink(sink):
        event('erp.stage', source='erp', synthetic=False, stage='download', body='hidden-marker-9481')
        logging.getLogger('httpx').info('https://private.test?token=hidden-marker-9481')
        logging.getLogger('uvicorn.error').error('hidden-marker-9481', exc_info=ValueError('hidden-marker-9481'))
    assert 'hidden-marker-9481' not in sink.path.read_text() + capsys.readouterr().err


def test_rotation_retention_and_active_files(tmp_path):
    sink = configure_logging(config(tmp_path, log_max_bytes=4096))
    with bind_sink(sink):
        for _ in range(35):
            event('http.completed', route='/api/overview', http_status=200)
    assert list(sink.path.parent.glob('*.jsonl.*'))
    assert len(records(sink)) == 35
    old = sink.path.parent / f'log-99999999-{uuid4().hex}.jsonl'
    old.write_text('{}\n')
    os.utime(old, (0, 0))
    active = sink.path.parent / f'log-{os.getpid()}-{uuid4().hex}.jsonl'
    active.write_text('{}\n')
    os.utime(active, (0, 0))
    outside = tmp_path / 'business.json'
    outside.write_text('must stay')
    link = sink.path.parent / f'log-99999999-{uuid4().hex}.jsonl'
    link.symlink_to(outside)
    sink.cleanup()
    assert not old.exists() and active.exists()
    assert outside.read_text() == 'must stay' and link.is_symlink()


def test_disk_error_falls_back_and_can_recover(tmp_path, monkeypatch, capsys):
    sink = configure_logging(config(tmp_path))
    real_write = os.write
    def full_disk(*args):
        raise OSError('secret exception detail')
    with bind_sink(sink), monkeypatch.context() as patch:
        patch.setattr(os, 'write', full_disk)
        assert event('http.failed', exception=ValueError('secret'))
    assert sink.status()['write_ok'] is False
    assert 'secret' not in capsys.readouterr().err
    assert os.write is real_write
    with bind_sink(sink):
        event('app.started')
    assert sink.status()['write_ok'] is True


def test_symlink_directory_is_not_written(tmp_path, capsys):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'logs').symlink_to(outside, target_is_directory=True)
    sink = configure_logging(config(tmp_path))
    with bind_sink(sink):
        event('app.started')
    assert sink.write_ok is False and not list(outside.iterdir())
    assert '运行日志保存异常' in capsys.readouterr().err


def test_parallel_context_isolation(tmp_path):
    sink = configure_logging(config(tmp_path))
    identifiers = [uuid4().hex for _ in range(20)]
    def write(identifier):
        with bind_sink(sink), bind_context(request_id=identifier):
            event('http.completed')
        assert current_context() == {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(write, identifiers))
    assert {row['request_id'] for row in records(sink)} == set(identifiers)


def test_separate_process_files_and_restart_readability(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = '''
import sys
from pathlib import Path
from types import SimpleNamespace
from app.observability import configure_logging,event
configure_logging(SimpleNamespace(runtime_dir=Path(sys.argv[1]),adapter_mode='demo'), 'capture')
for i in range(5): event('erp.stage', source='erp', synthetic=False, stage='download')
'''
    env = dict(os.environ, PYTHONPATH=str(root / 'backend'))
    processes = [subprocess.Popen([sys.executable, '-c', script, str(tmp_path)], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for process in processes:
        assert process.communicate(timeout=15)[0] == b''
        assert process.returncode == 0
    sink = Sink(config(tmp_path), 'api')
    assert len(list((sink.root / 'capture').glob('*.jsonl'))) == 2
    assert len(records(sink)) == 10


def test_http_error_has_generated_id_and_safe_body(tmp_path):
    app = create_app(config(tmp_path))
    @app.get('/api/test-log-error')
    def broken():
        raise RuntimeError('password-private-original-response')
    with TestClient(app) as client:
        response = client.get('/api/test-log-error?token=do-not-log', headers={'X-Request-ID': 'attacker-value'})
        assert response.status_code == 500
        assert len(response.headers['X-Request-ID']) == 32
        assert response.headers['X-Error-ID'] == response.json()['error_id']
        assert 'password-private' not in response.text
        response2 = client.get('/api/health')
        assert response.headers['X-Request-ID'] != response2.headers['X-Request-ID']
    text = app.state.log_sink.path.read_text()
    assert 'do-not-log' not in text and 'attacker-value' not in text and 'password-private' not in text


def test_request_to_job_context_persists_and_old_payload_works(tmp_path):
    cfg = config(tmp_path)
    app = create_app(cfg)
    request_id = uuid4().hex
    with bind_sink(app.state.log_sink), bind_context(request_id=request_id):
        job_id = enqueue(app.state.factory, 'run', {})
    with app.state.factory() as session:
        assert session.get(Job, job_id).payload['_log_context']['request_id'] == request_id
    class Service:
        config = cfg
        def reconcile(self):
            pass
        def run_batch(self, run_key):
            assert current_context()['request_id'] == request_id
            return None
    worker = Worker(app.state.factory, Service())
    worker.tick(100)
    row = next(row for row in records(worker.log_sink) if row['event'] == 'job.started')
    assert row['request_id'] == request_id and row['job_id'] == job_id
    assert current_context() == {}
    app.state.engine.dispose()


def test_job_failure_has_safe_error_id_and_does_not_retry_automatically(tmp_path):
    cfg = config(tmp_path)
    app = create_app(cfg)
    job_id = enqueue(app.state.factory, 'run', {})
    class Service:
        config = cfg
        calls = 0
        def reconcile(self):
            pass
        def run_batch(self, key):
            self.calls += 1
            raise RuntimeError('secret-db-password')
    service = Service()
    worker = Worker(app.state.factory, service)
    worker.tick(100)
    worker.tick(105)
    with app.state.factory() as session:
        row = session.get(Job, job_id)
        assert row.state == 'failed' and '排查编号' in row.error and 'secret' not in row.error
    assert service.calls == 1
    app.state.engine.dispose()


def test_bootstrap_configuration_errors_hide_input(tmp_path, monkeypatch):
    import app.config as configuration
    class IsolatedSettings(Settings):
        model_config = {**Settings.model_config, 'env_file': None}
    monkeypatch.setattr(configuration, 'Settings', IsolatedSettings)
    monkeypatch.setenv('LR_LOG_LEVEL', 'private-invalid-setting-marker')
    with pytest.raises(RuntimeError) as error:
        configuration.get_settings()
    assert 'private-invalid-setting-marker' not in str(error.value)
    assert 'log_level' in str(error.value)


def test_worker_entry_records_shutdown_and_disposes_engine(tmp_path, monkeypatch):
    import app.main as entry
    import app.worker as worker_entry
    cfg = config(tmp_path)
    sink = configure_logging(cfg, 'worker')
    disposed = []
    fake = SimpleNamespace(state=SimpleNamespace(
        factory=None, service=SimpleNamespace(config=cfg), config=cfg, log_sink=sink,
        engine=SimpleNamespace(dispose=lambda: disposed.append(True))))
    monkeypatch.setattr(entry, 'create_app', lambda **kwargs: fake)
    def stop(self, seconds):
        raise RuntimeError('stopped')
    monkeypatch.setattr(worker_entry.Worker, 'run_forever', stop)
    with pytest.raises(RuntimeError, match='stopped'):
        worker_entry.main()
    assert disposed == [True]
    assert records(sink)[-1]['event'] == 'app.stopped'


def test_logging_settings_bounds(tmp_path):
    with pytest.raises(ValueError):
        config(tmp_path, log_level='RAW')
    with pytest.raises(ValueError):
        config(tmp_path, log_retention_days=0)
