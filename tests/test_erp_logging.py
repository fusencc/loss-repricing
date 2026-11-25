"""ERP日志旁路故障、原始异常定位和独立桌面安全日志；仅使用合成数据。"""
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.adapters.base import SourceUnavailable
from app.config import Settings
from app.observability import bind_sink, configure_logging
from test_erp_bulk_capture import run
from test_erp_desktop import load_desktop


def _records(root):
    return [json.loads(line) for path in root.rglob('*.jsonl')
            for line in path.read_text(encoding='utf-8').splitlines()]


@pytest.mark.parametrize('issue', [None, 'rate'])
@pytest.mark.parametrize('standalone', [False, True])
def test_callback_failure_never_changes_capture_flow(tmp_path, monkeypatch, capsys, issue, standalone):
    if standalone:
        import test_erp_bulk_capture as contract
        _, desktop = load_desktop(tmp_path / 'standalone')
        monkeypatch.setattr(contract, 'capture_erp_bulk', desktop.capture_erp_bulk)
    observed = []

    def broken_event(name, **fields):
        observed.append(name)
        raise OSError('private-callback-path-and-token')

    result, calls = run(tmp_path / 'business', issue=issue, event_callback=broken_event)
    assert result['capture_complete'] is (issue is None)
    assert result['rate_limit_observed'] is (issue == 'rate')
    assert result['pricing_ready'] is False
    assert Path(result['result_file']).is_file()
    assert len(observed) > 1
    assert calls.count('/newIp/report/orderDownload/asyncOrderListDownload') == 1
    if issue == 'rate':
        assert calls.count('/newIp/report/download/list') == 1
    output = capsys.readouterr()
    assert output.err.count('ERP技术日志暂不可用') == 1
    assert 'private-callback' not in output.err and 'Traceback' not in output.err


@pytest.mark.parametrize('entry, adapter, error_type, kwargs, status, detail, stage', [
    ('sample', 'capture_erp_sample', SourceUnavailable, {'limit': 1}, 503, 'private-source-failure', 'collect'),
    ('rates', 'query_erp_rates', SourceUnavailable, {}, 503, 'private-source-failure', 'rates'),
    ('capture', 'capture_erp_bulk', SourceUnavailable, {'query': {}}, 503, 'private-source-failure', 'collect'),
    ('capture', 'save_capture', OSError, {'query': {}}, 503, 'ERP采集文件未能确认保存，请核实服务运行目录', 'save'),
    ('read_capture', 'load_capture', OSError, {'capture_id': 'not-logged'}, 503, '采集文件读取失败', 'read'),
    ('read_capture', 'load_capture', FileNotFoundError, {'capture_id': 'not-logged'}, 404, '未找到采集文件或本轮订单', 'read'),
    ('read_capture', 'load_capture', ValueError, {'capture_id': 'not-logged'}, 422, '采集编号或文件内容无效', 'read'),
])
def test_api_failure_keeps_original_source_location_and_one_error_id(
        tmp_path, monkeypatch, entry, adapter, error_type, kwargs, status, detail, stage):
    import app.api.erp_rates as routes
    settings = Settings(_env_file=None, runtime_dir=tmp_path)
    sink = configure_logging(settings, role='api')
    request = Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(config=settings))})

    def fail_source(*args, **options):
        raise error_type('private-source-failure')

    # 保存失败测试先提供合成读取结果，避免进入真实请求。
    if adapter == 'save_capture':
        monkeypatch.setattr(routes, 'capture_erp_bulk', lambda *args, **options: {})
    monkeypatch.setattr(routes, adapter, fail_source)
    kwargs = dict(kwargs, query=routes.CaptureQuery(**kwargs['query'])) if 'query' in kwargs else kwargs
    with bind_sink(sink), pytest.raises(HTTPException) as caught:
        getattr(routes, entry)(request, **kwargs)
    assert caught.value.status_code == status and caught.value.detail == detail
    assert caught.value.headers['X-Error-ID'] == request.state.error_id
    failures = [record for record in _records(tmp_path / 'logs') if record['event'] == 'erp.failed']
    assert len(failures) == 1
    record = failures[0]
    assert record['error_id'] == request.state.error_id
    assert record['exception_type'] == error_type.__name__
    assert record['source'] == 'erp' and record['synthetic'] is False
    assert record['stage'] == stage and record['http_status'] == status
    assert any(frame.get('function') == 'fail_source' and frame.get('line') == fail_source.__code__.co_firstlineno + 1
               for frame in record['stack'])
    assert 'private-source-failure' not in json.dumps(record)


def test_api_storage_failure_reuses_persisted_error_id(tmp_path, monkeypatch):
    import app.api.erp_rates as routes
    import app.services.erp_capture_store as store
    settings = Settings(_env_file=None, runtime_dir=tmp_path)
    sink = configure_logging(settings, role='api')
    request = Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(config=settings))})
    monkeypatch.setattr(routes, 'capture_erp_bulk', lambda *args, **kwargs: {'acquisition_mode': 'bulk_download'})

    def fail_write(*args):
        raise OSError('private-storage-path')

    monkeypatch.setattr(store, 'write_verified', fail_write)
    with bind_sink(sink), pytest.raises(HTTPException) as caught:
        routes.capture(request, routes.CaptureQuery())
    assert caught.value.status_code == 503
    failures = [record for record in _records(tmp_path / 'logs')
                if record['event'] in ('erp.failed', 'capture.failed')]
    assert len(failures) == 1 and failures[0]['event'] == 'capture.failed'
    assert failures[0]['error_id'] == caught.value.headers['X-Error-ID'] == request.state.error_id
    assert failures[0]['stack'][-1]['function'] == 'fail_write'
    assert 'private-storage' not in json.dumps(failures)


@pytest.mark.parametrize('failure_phase', ['setup', 'emit'])
def test_desktop_log_io_failure_degrades_without_raw_traceback(tmp_path, monkeypatch, capsys, failure_phase):
    _, desktop = load_desktop(tmp_path / 'standalone')

    @contextmanager
    def unavailable(*args):
        raise OSError('private-output-folder')
        yield

    if failure_phase == 'setup':
        monkeypatch.setattr(desktop, '_desktop_log_directory', unavailable)
    safe_event = desktop._desktop_logger(tmp_path / 'output')
    if failure_phase == 'emit':
        monkeypatch.setattr(desktop, '_desktop_log_directory', unavailable)
    error_id = safe_event('erp.failed', exception=OSError('private-raw-exception'), stage='auth')
    safe_event('erp.stage', stage='save')
    assert isinstance(error_id, str) and len(error_id) == 32
    output = capsys.readouterr()
    assert output.err.count('桌面技术日志暂不可用') == 1
    assert 'private-' not in output.err and 'Traceback' not in output.err


def test_desktop_write_failure_never_uses_raw_logging_traceback(tmp_path, monkeypatch, capsys):
    _, desktop = load_desktop(tmp_path / 'standalone')
    safe_event = desktop._desktop_logger(tmp_path / 'output')

    def fail_write(*args):
        raise OSError('private-disk-failure')

    with monkeypatch.context() as scoped:
        scoped.setattr(desktop.os, 'write', fail_write)
        assert safe_event('erp.failed', exception=OSError('private-source-failure'), stage='poll')
        safe_event('erp.stage', stage='save')
    output = capsys.readouterr()
    assert output.err.count('桌面技术日志暂不可用') == 1
    assert 'private-' not in output.err and 'Traceback' not in output.err


def test_desktop_main_continues_when_logger_setup_fails(tmp_path, monkeypatch, capsys):
    path, desktop = load_desktop(tmp_path / 'standalone')

    @contextmanager
    def unavailable(*args):
        raise OSError('private-log-directory')
        yield

    monkeypatch.setattr(desktop, '_desktop_log_directory', unavailable)
    monkeypatch.setattr(desktop.getpass, 'getpass', lambda prompt: 'private-password')
    monkeypatch.setattr('sys.argv', [str(path), '--username', 'synthetic-user', '--output', str(tmp_path / 'output')])
    monkeypatch.setattr(desktop, 'capture_erp_bulk', lambda *args, **kwargs: {
        'summary': {}, 'groups': [{'error': None}], 'sample_only': True,
        'selection_complete': True, 'capture_complete': False, 'result_file': 'synthetic.json'})
    assert desktop.desktop_main() == 0
    output = capsys.readouterr()
    assert output.err.count('桌面技术日志暂不可用') == 1
    assert 'private-' not in output.err + output.out and 'Traceback' not in output.err


@pytest.mark.parametrize('link_parent', [False, True])
def test_desktop_rejects_symlink_log_directory(tmp_path, capsys, link_parent):
    _, desktop = load_desktop(tmp_path / 'standalone')
    outside = tmp_path / 'outside'
    outside.mkdir()
    output = tmp_path / 'output'
    if link_parent:
        output.symlink_to(outside, target_is_directory=True)
    else:
        output.mkdir()
        (output / 'logs').symlink_to(outside, target_is_directory=True)
    safe_event = desktop._desktop_logger(output)
    safe_event('erp.started', stage='auth')
    assert not list(outside.rglob('*'))
    assert '桌面技术日志暂不可用' in capsys.readouterr().err


def test_desktop_rejects_replaced_log_file_symlink(tmp_path, capsys):
    _, desktop = load_desktop(tmp_path / 'standalone')
    safe_event = desktop._desktop_logger(tmp_path / 'output')
    safe_event('erp.started', stage='auth')
    path = next((tmp_path / 'output' / 'logs').glob('*.jsonl'))
    outside = tmp_path / 'private.txt'
    outside.write_text('untouched', encoding='utf-8')
    path.unlink()
    path.symlink_to(outside)
    safe_event('erp.stage', stage='poll')
    assert outside.read_text() == 'untouched'
    assert '桌面技术日志暂不可用' in capsys.readouterr().err


def test_desktop_instances_have_separate_files_and_safe_failure_stack(tmp_path):
    _, desktop = load_desktop(tmp_path / 'standalone')
    first = desktop._desktop_logger(tmp_path / 'output')
    second = desktop._desktop_logger(tmp_path / 'output')
    first('erp.started', stage='auth', count=1.25, arbitrary='private-ignored')
    second('erp.started', stage='auth')
    try:
        raise OSError('private-raw-exception')
    except OSError as error:
        error_id = first('erp.failed', exception=error, stage='poll')
    paths = list((tmp_path / 'output' / 'logs').glob('capture-*.jsonl'))
    assert len(paths) == 2
    records = _records(tmp_path / 'output' / 'logs')
    assert len({record['operation_id'] for record in records}) == 2
    failure = next(record for record in records if record['event'] == 'erp.failed')
    assert failure['error_id'] == error_id
    assert failure['stack'][-1]['file'] == 'test_erp_logging.py'
    assert all('count' not in record for record in records)
    assert 'private-' not in json.dumps(records)


def test_desktop_rotation_stays_with_own_instance(tmp_path):
    _, desktop = load_desktop(tmp_path / 'standalone')
    safe_event = desktop._desktop_logger(tmp_path / 'output')
    safe_event('erp.started', stage='auth')
    path = next((tmp_path / 'output' / 'logs').glob('*.jsonl'))
    path.write_bytes(b'x' * (10 * 1024 * 1024))
    safe_event('erp.stage', stage='save')
    assert path.with_name(path.name + '.1').stat().st_size == 10 * 1024 * 1024
    assert json.loads(path.read_text())['stage'] == 'save'
