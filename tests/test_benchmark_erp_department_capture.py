import importlib.util
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from openpyxl import Workbook
import pytest

PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'benchmark_erp_department_capture.py'
spec = importlib.util.spec_from_file_location('department_benchmark', PATH)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def workbook(ids, department='Tiktok一部'):
    book = Workbook()
    book.active.append(['订单编号', '部门', '毛利'])
    for order in ids:
        book.active.append([order, department, -1])
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def config(interval=0):
    return {'username': 'test-user', 'password': 'secret-password', 'platform': 'Tiktok',
            'platform_id': 'platform-id', 'bloc': '集团二部', 'interval': interval}


def success(data, code=200):
    return httpx.Response(200, json={'code': code, 'data': data})


def args(max_orders=None, batch_size=1000):
    return SimpleNamespace(max_orders=max_orders, batch_size=batch_size)


@pytest.mark.parametrize('source', list(bench.SOURCES))
def test_department_filters_exact_task_and_download_privacy(tmp_path, source):
    raw = workbook(['O-1'])
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == bench.LOGIN:
            assert json.loads(request.content) == {'username': 'test-user', 'password': 'secret-password'}
            return success({'accessToken': 'private-token'})
        if path == bench.LOSS:
            body = json.loads(request.content)
            assert body['department'] == 'Tiktok一部'
            assert body['financecheck'] == '3'
            return httpx.Response(200, content=raw)
        if path == bench.BLOCKED:
            body = json.loads(request.content)
            assert body['departments'] == 'Tiktok一部'
            assert body['jzfh'] == '1'
            return success({'records': [{'orderid': 'O-1'}], 'total': 1}, code=0)
        if path == bench.EXPORT:
            assert json.loads(request.content) == {'orderIdList': ['O-1']}
            return success('已创建下载任务【exact-task】')
        if path == bench.TASKS:
            return success({'records': [
                {'source': 'orderList', 'fileName': 'other-task', 'status': 'success',
                 'downloadUrl': 'http://bad.invalid/wrong'},
                {'source': 'orderList', 'fileName': 'exact-task', 'status': 'success',
                 'downloadUrl': 'http://103.198.125.2:19001/file?signature=private-url'},
            ]})
        assert request.url.port == 19001
        assert 'authorization' not in request.headers
        return httpx.Response(200, content=raw)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        erp = bench.Erp(config(), client)
        erp.login()
        group = bench.collect_group(erp, 'Tiktok一部', source, tmp_path, args(), lambda _: None)
    assert group['status'] == 'complete'
    assert group['candidate_orders'] == group['downloaded_unique_orders'] == 1
    assert group['finance_rows'] == 1
    assert group['missing_order_ids'] == []
    assert all(r.headers['authorization'] == 'private-token' for r in requests[1:-1])
    text = (tmp_path / 'result.json').read_text()
    assert 'private-token' not in text and 'private-url' not in text and 'secret-password' not in text
    assert len(list(tmp_path.glob('订单明细-*.xlsx'))) == 1
    assert {'login', 'candidate_collection', 'export_submit', 'task_wait', 'download', 'parse'} <= set(erp.stats['stages'])


def test_dynamic_totals_duplicate_orders_and_sample_limit(tmp_path):
    def handler(request):
        body = json.loads(request.content) if request.method == 'POST' else {}
        if request.url.path == bench.BLOCKED:
            if body['page'] == 1:
                return success({'records': [{'orderid': f'O-{i}'} for i in range(100)], 'total': 200}, 0)
            return success({'records': [{'orderid': 'O-99'}, {'orderid': 'O-100'}], 'total': 102}, 0)
        if request.url.path == bench.EXPORT:
            assert body['orderIdList'] == ['O-0']
            return success('下载任务【task】')
        if request.url.path == bench.TASKS:
            return success([{'source': 'orderList', 'fileName': 'task', 'status': 'success', 'downloadUrl': '/file'}])
        return httpx.Response(200, content=workbook(['O-0']))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        group = bench.collect_group(bench.Erp(config(), client), 'Tiktok一部', 'blocked_pending',
                                    tmp_path, args(max_orders=1), lambda _: None)
    assert group['status'] == 'complete'
    assert group['candidate_orders'] == 101
    assert group['requested_orders'] == 1
    assert [p['total_reported'] for p in group['pages']] == [200, 102]


def test_blocked_page_retries_after_16_and_32_seconds(tmp_path):
    clock = Clock()
    calls = []

    def handler(request):
        assert request.url.path == bench.BLOCKED
        calls.append((clock.now(), json.loads(request.content)))
        if len(calls) == 1:
            raise httpx.ReadTimeout('mock timeout', request=request)
        if len(calls) == 2:
            return httpx.Response(503)
        return success({'records': [], 'total': 0}, 0)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        erp = bench.Erp(config(interval=16), client, clock=clock.now, sleep=clock.sleep)
        group = bench.collect_group(erp, 'Tiktok一部', 'blocked_pending', tmp_path, args(), lambda _: None)

    assert group['status'] == 'complete'
    assert [time for time, _ in calls] == [0, 16, 48]
    assert all(body == calls[0][1] for _, body in calls)


def test_failed_later_page_preserves_orders_and_failed_batch_continues(tmp_path):
    submitted = []
    clock = Clock()
    failed_page_attempts = []

    def handler(request):
        body = json.loads(request.content) if request.method == 'POST' else {}
        if request.url.path == bench.BLOCKED:
            if body['page'] == 1:
                return success({'records': [{'orderid': f'O-{i}'} for i in range(100)], 'total': 300}, 0)
            failed_page_attempts.append(clock.now())
            raise httpx.ReadTimeout('http://private.invalid?secret=hidden', request=request)
        if request.url.path == bench.EXPORT:
            submitted.append(body['orderIdList'])
            if len(submitted) == 1:
                return httpx.Response(503, text='private-response')
            return success('下载任务【second】')
        if request.url.path == bench.TASKS:
            return success([{'source': 'orderList', 'fileName': 'second', 'status': 'success', 'downloadUrl': '/file'}])
        return httpx.Response(200, content=workbook(submitted[-1]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        erp = bench.Erp(config(), client, clock=clock.now, sleep=clock.sleep)
        group = bench.collect_group(erp, 'Tiktok一部', 'blocked_allocatable',
                                    tmp_path, args(batch_size=50), lambda _: None)
    assert failed_page_attempts == [0, 16, 48]
    assert group['candidate_orders'] == 100
    assert group['list_complete'] is False
    assert group['status'] == 'partial'
    assert [b['status'] for b in group['batches']] == ['failed', 'downloaded']
    assert len(submitted) == 2  # 失败或结果未知的创建请求不重提。
    assert group['downloaded_unique_orders'] == 50
    assert len(group['missing_order_ids']) == 50
    assert 'hidden' not in json.dumps(group) and 'private-response' not in json.dumps(group)


def test_zero_orders_are_success(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda r: success({'records': [], 'total': 0}, 0))) as client:
        group = bench.collect_group(bench.Erp(config(), client), 'Tiktok自定义部', 'blocked_pending',
                                    tmp_path, args(), lambda _: None)
    assert group['status'] == 'complete'
    assert group['batches'] == []
    assert group['candidate_orders'] == 0


def test_timing_separates_http_throttle_and_stage():
    clock = Clock()

    def handler(request):
        clock.sleep(0.5)
        return success({})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        erp = bench.Erp(config(interval=16), client, clock=clock.now, sleep=clock.sleep)
        with erp.stage('task_wait'):
            erp.post(bench.TASKS, {})
            erp.post(bench.TASKS, {})
    assert erp.stats['requests'] == 2
    assert erp.stats['http_seconds'] == 1
    assert erp.stats['throttle_seconds'] == 15.5
    assert erp.stats['stages']['task_wait'] == 16.5


def test_numeric_ids_not_used_and_department_differences_reported():
    info = bench.inspect_xlsx(workbook(['text', 1234567890123456], 'Tiktok五部'), 'Tiktok一部')
    assert info['order_ids'] == ['text']
    assert info['invalid_order_id_rows'] == 1
    assert info['department_mismatch_rows'] == 2


def test_config_uses_only_explicit_project_file_and_environment(tmp_path, monkeypatch):
    parent = tmp_path / '.env'
    parent.write_text('LR_ERP_PASSWORD=parent-secret\n')
    project = tmp_path / 'project'
    project.mkdir()
    env = project / '.env'
    env.write_text('LR_ERP_USERNAME=file-user\nLR_ERP_PASSWORD=project-secret\n')
    for key in ('LR_ERP_USERNAME', 'LR_ERP_PASSWORD', 'LR_ERP_CAPTURE_REQUEST_INTERVAL_SECONDS'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('LR_ERP_USERNAME', 'environment-user')
    cfg = bench.load_config(env)
    assert cfg['username'] == 'environment-user'
    assert cfg['password'] == 'project-secret'
    env.unlink()
    assert bench.load_config(env)['password'] == ''


def test_poll_only_uses_matching_task_and_does_not_resubmit():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return success([{'source': 'orderList', 'fileName': 'someone-else', 'status': 'success',
                         'downloadUrl': 'http://private.invalid?secret=value'}])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(bench.CaptureError, match='不自动重新提交'):
            bench.wait_task(bench.Erp(config(), client), 'this-task', lambda _: None, max_polls=2)
    assert paths == [bench.TASKS, bench.TASKS]


def test_main_multiple_departments_cross_source_summary_and_safe_config(tmp_path, monkeypatch):
    client_class = httpx.Client
    bodies = []
    raw = workbook(['O-1'])

    def handler(request):
        path = request.url.path
        if path == bench.LOGIN:
            return success({'token': 'private-token'})
        if path == bench.LOSS:
            body = json.loads(request.content)
            bodies.append(body)
            return httpx.Response(200, content=workbook(['O-1'], body['department']))
        if path == bench.EXPORT:
            return success('下载任务【mine】')
        if path == bench.TASKS:
            return success([{'source': 'orderList', 'fileName': 'mine', 'status': 'success',
                             'downloadUrl': 'http://storage.invalid/file?signature=secret-url'}])
        return httpx.Response(200, content=raw)

    monkeypatch.setattr(bench, 'load_config', lambda: config())
    monkeypatch.setattr(bench.httpx, 'Client', lambda **kwargs: client_class(
        transport=httpx.MockTransport(handler), **kwargs))
    result = bench.main(['--department', 'Tiktok一部', '--department', 'Tiktok五部',
                         '--source', 'loss_pending', '--source', 'loss_allocatable',
                         '--output', str(tmp_path)])
    report_file = next(tmp_path.glob('erp-department-*/result.json'))
    report = json.loads(report_file.read_text())
    assert report['summary']['candidate_unique_order_ids'] == 1
    assert report['summary']['downloaded_unique_order_ids'] == 1
    assert [b['department'] for b in bodies] == ['Tiktok一部', 'Tiktok一部', 'Tiktok五部', 'Tiktok五部']
    assert len(report['groups']) == 4
    assert report['status'] == 'partial' and result == 1  # 五部回读到一部，报告异常但四组均已下载。
    assert report['timing']['requests'] == 17
    for path in report_file.parent.rglob('*.json'):
        text = path.read_text()
        assert 'private-token' not in text and 'secret-password' not in text and 'secret-url' not in text


def test_rate_limit_is_local_failure_not_global_circuit_breaker(tmp_path):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return success(None, 500003)
        return success({'records': [], 'total': 0}, 0)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        erp = bench.Erp(config(), client)
        first = bench.collect_group(erp, 'Tiktok一部', 'blocked_pending', tmp_path / 'one', args(), lambda _: None)
        second = bench.collect_group(erp, 'Tiktok五部', 'blocked_pending', tmp_path / 'two', args(), lambda _: None)
    assert first['status'] == 'partial'
    assert second['status'] == 'complete'
    assert erp.stats['rate_limit_hits'] == 1
