"""动态名单及已受理导出遇到读取异常时保留已取得的数据。"""
import json
from io import BytesIO

import httpx
import pytest
from openpyxl import Workbook

from app.adapters import sales_batch_export as sales
from app.adapters.erp_bulk_capture import BLOCKED_PAGE_SIZE, _blocked_ids
from app.adapters.sales_auth import SalesAuth
from app.config import Settings


def test_dynamic_page_overlap_keeps_unique_orders_and_continues_to_next_page():
    requested = []
    size = BLOCKED_PAGE_SIZE
    def post(path, body):
        page = body['page']
        requested.append(page)
        assert body['limit'] == size
        # 新记录插入使第二页回读第一页面尾；满页仍须继续，不能按去重后行数结束。
        indices = {1: range(size), 2: range(size - 2, 2 * size - 2),
                   3: range(2 * size - 2, 2 * size + 1)}[page]
        return {'total': 2 * size + 3, 'records': [
            {'orderid': f'B{i}', 'status': 4, 'platform': 'Tiktok', 'jzfh': '1'}
            for i in indices]}
    group = {'status': 4, 'filters': {}, 'pages': [], 'total': None}
    assert _blocked_ids(post, group) == [f'B{i}' for i in range(2 * size + 1)]
    assert requested == [1, 2, 3]
    assert group['pages'][1]['duplicate_rows'] == 2
    assert group['pages'][1]['raw_rows'] == size
    assert group['total'] == 2 * size + 1
    assert not group.get('excluded_rows') and 'list_error' not in group


def test_duplicate_out_of_scope_record_is_excluded_without_discarding_valid_rows():
    size = BLOCKED_PAGE_SIZE
    def post(path, body):
        rows = [{'orderid': f'B{i}', 'status': 4, 'platform': 'Tiktok', 'jzfh': '1'}
                for i in range(size)]
        if body['page'] == 2:
            rows = [dict(rows[-1], platform='Shopee'), dict(rows[-1], orderid=f'B{size}')]
        return {'total': size + 2, 'records': rows}
    group = {'status': 4, 'filters': {}, 'pages': [], 'total': None}
    assert _blocked_ids(post, group) == [f'B{i}' for i in range(size + 1)]
    assert group['total'] == size + 1
    excluded, = group['excluded_rows']
    assert excluded['validation_reason'] == 'platform_mismatch'
    assert excluded['order_id'] == f'B{size - 1}'
    assert excluded['page'] == 2 and excluded['row_number'] == 1
    assert group['pages'][1]['excluded_rows'] == 1
    assert group['pages'][1]['duplicate_rows'] == 0
    assert 'list_error' not in group and not group.get('failed_pages')


@pytest.mark.parametrize('failure', ['http400', 'business', 'timeout', 'http503'])
def test_accepted_export_survives_query_failure_without_resubmission(monkeypatch, failure):
    monkeypatch.setattr(sales, 'sleep', lambda _: None)
    product = '1234567890123456701'
    book = Workbook(); sheet = book.active; sheet.append(sales.COLUMNS)
    row = {'产品ID': product, '集团': '集团二部', '商品ID': product + '0'}
    sheet.append([row.get(column) for column in sales.COLUMNS])
    buffer = BytesIO(); book.save(buffer); book.close()
    counts = {'export': 0, 'tasks': 0}
    checkpoints = []
    def route(request):
        path = request.url.path
        if path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'test', 'expires_in': 3600})
        if path == sales.EXPORT:
            counts['export'] += 1
            return httpx.Response(200, json={'resp_code': 0, 'datas': None})
        if path == sales.TASKS:
            counts['tasks'] += 1
            if counts['tasks'] == 2:
                if failure == 'timeout':
                    raise httpx.ReadTimeout('read failed', request=request)
                if failure == 'business':
                    return httpx.Response(200, json={'resp_code': 500, 'resp_msg': '系统繁忙'})
                return httpx.Response(400 if failure == 'http400' else 503,
                                      json={'resp_code': 500, 'resp_msg': '系统繁忙'})
            rows = [] if counts['tasks'] == 1 else [
                {'id': 'bound-task', 'taskType': 1, 'status': 3,
                 'excelUrl': 'http://121.46.237.218:19110/export.xlsx'}]
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': rows}})
        return httpx.Response(200, content=buffer.getvalue())
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        auth = SalesAuth(Settings(_env_file=None, vendor_username='u', vendor_password='p',
                                  sales_request_interval_seconds=0), client)
        content, task, rows = sales._export_file(
            client, auth, [product], '集团二部',
            checkpoint=lambda d: checkpoints.append(json.loads(json.dumps(d))))
    assert content == buffer.getvalue() and task == 'bound-task'
    assert [r['产品ID'] for _, r in rows] == [product]
    assert counts['export'] == 1
    assert any(d['state'] == 'poll_retry' for d in checkpoints)
    assert checkpoints[-1]['state'] == 'downloaded'
    assert checkpoints[-1]['task_id'] == 'bound-task'
    assert 'excelUrl' not in json.dumps(checkpoints)
    assert 'test' not in json.dumps(checkpoints)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_export_query_auth_and_rate_limit_stop_without_resubmission(status, monkeypatch):
    monkeypatch.setattr(sales, 'sleep', lambda _: pytest.fail('must not retry rejected authentication'))
    calls = []
    checkpoints = []
    def route(request):
        path = request.url.path; calls.append(path)
        if path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'private-token', 'expires_in': 3600})
        if path == sales.EXPORT:
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'id': 'bound'}})
        if calls.count(sales.TASKS) == 1:
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': []}})
        return httpx.Response(status, json={'resp_code': status})
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        auth = SalesAuth(Settings(_env_file=None, vendor_username='u', vendor_password='p',
                                  sales_request_interval_seconds=0), client)
        with pytest.raises(httpx.HTTPStatusError):
            sales._export_file(client, auth, ['P1'], '集团二部',
                               checkpoint=lambda d: checkpoints.append(json.loads(json.dumps(d))))
    assert calls.count(sales.EXPORT) == 1 and calls.count(sales.TASKS) == 2
    assert auth.stopped_http_status == status
    assert checkpoints[-1]['task_id'] == 'bound'
    assert checkpoints[-1]['read_failures'][-1]['http_status'] == status


def test_completed_batch_is_saved_before_next_batch_and_survives_later_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sales, 'BATCH_SIZE', 1)
    monkeypatch.setattr(sales, 'sleep', lambda _: None)
    products = ['1234567890123456701', '1234567890123456702']
    book = Workbook(); sheet = book.active; sheet.append(sales.COLUMNS)
    row = {'产品ID': products[0], '集团': '集团二部', '商品ID': 'I1', '站点': 'PH',
           '店铺名称': 'SHOP', '本地主sku': 'MAIN', '本地子sku': 'SKU', '平台子sku': 'SKU'}
    sheet.append([row.get(column) for column in sales.COLUMNS])
    buffer = BytesIO(); book.save(buffer); book.close()
    submitted = []
    def route(request):
        path = request.url.path
        if path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'test-token', 'expires_in': 3600})
        if path == sales.EXPORT:
            submitted.append(json.loads(request.content)['productIdStr'])
            if len(submitted) == 2:
                report = json.loads(next(tmp_path.glob('sales-source-captures/*/result.json')).read_text())
                assert report['summary']['variants'] == 1
                assert Path(report['files'][0]['file']).read_bytes() == buffer.getvalue()
                assert not report['acquisition_complete']
                return httpx.Response(403, json={'resp_code': 403})
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'id': 'first'}})
        if path == sales.TASKS:
            rows = [] if not submitted else [{'id': 'first', 'taskType': 1, 'status': 3,
                        'excelUrl': 'http://121.46.237.218:19110/export.xlsx'}]
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': rows}})
        return httpx.Response(200, content=buffer.getvalue())
    from pathlib import Path
    capture = {'capture_id': 'capture', 'groups': []}
    candidates = {'O1': [products[0]], 'O2': [products[1]]}
    scope = {key: {'erp_shop': 'SHOP', 'shop_id': 'SHOP-ID'} for key in candidates}
    settings = Settings(_env_file=None, runtime_dir=tmp_path, vendor_username='u', vendor_password='p',
                        sales_request_interval_seconds=0)
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        result = sales.capture_sales_batch(settings, capture, candidates, scope, client=client)
    assert len(result['files']) == len(result['variants']) == 1
    assert not result['acquisition_complete']
    assert result['issues'][0]['stage'] == 'submit'
    assert len(result['export_tasks']) == 2
    assert json.loads(Path(result['export_tasks'][0]).read_text())['state'] == 'downloaded'
    assert json.loads(Path(result['export_tasks'][1]).read_text())['state'] == 'submission_unconfirmed'


def test_unconfirmed_export_creation_is_never_retried(monkeypatch):
    calls = []
    evidence = []
    def route(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'private', 'expires_in': 3600})
        if request.url.path == sales.TASKS:
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': []}})
        raise httpx.ReadTimeout('unknown creation result', request=request)
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        auth = SalesAuth(Settings(_env_file=None, vendor_username='u', vendor_password='p',
                                  sales_request_interval_seconds=0), client)
        with pytest.raises(httpx.ReadTimeout):
            sales._export_file(client, auth, ['P1'], '集团二部',
                               checkpoint=lambda d: evidence.append(json.loads(json.dumps(d))))
    assert calls.count(sales.EXPORT) == 1
    assert evidence[-1]['state'] == 'submission_unconfirmed'
    assert evidence[-1]['stage'] == 'submit'


@pytest.mark.parametrize('failure_at', [0, 85])
def test_persistent_query_failure_is_bounded_and_keeps_bound_task(monkeypatch, failure_at):
    clock = [0]
    monkeypatch.setattr(sales, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(sales, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls = []
    query_times = []
    evidence = []
    def route(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'private', 'expires_in': 3600})
        if request.url.path == sales.EXPORT:
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'id': 'original-task'}})
        if calls.count(sales.TASKS) == 1:
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': []}})
        query_times.append(clock[0])
        if clock[0] < failure_at:
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': []}})
        return httpx.Response(400, json={'resp_code': 500})
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        auth = SalesAuth(Settings(_env_file=None, vendor_username='u', vendor_password='p',
                                  sales_request_interval_seconds=0), client)
        with pytest.raises(httpx.HTTPStatusError):
            sales._export_file(client, auth, ['P1'], '集团二部',
                               checkpoint=lambda d: evidence.append(json.loads(json.dumps(d))))
    assert calls.count(sales.EXPORT) == 1
    assert query_times == list(range(0, 90, 5))
    assert len(evidence[-1]['read_failures']) == (90 - failure_at) // 5
    assert evidence[-1]['state'] == 'read_failed'
    assert evidence[-1]['task_id'] == 'original-task'


@pytest.mark.parametrize('status', [401, 403, 429])
def test_download_rejection_sets_shared_collection_stop(status, monkeypatch):
    monkeypatch.setattr(sales, 'sleep', lambda _: pytest.fail('rejected download must not retry'))
    submitted = []
    def route(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'private', 'expires_in': 3600})
        if request.url.path == sales.EXPORT:
            submitted.append(True)
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'id': 'bound'}})
        if request.url.path == sales.TASKS:
            rows = [] if not submitted else [{'id': 'bound', 'taskType': 1, 'status': 3,
                        'excelUrl': 'http://121.46.237.218:19110/export.xlsx'}]
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': rows}})
        return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        auth = SalesAuth(Settings(_env_file=None, vendor_username='u', vendor_password='p',
                                  sales_request_interval_seconds=0), client)
        with pytest.raises(httpx.HTTPStatusError):
            sales._export_file(client, auth, ['P1'], '集团二部')
        assert auth.stopped_http_status == status


def test_oversized_file_stops_only_its_batch_without_repeated_download(tmp_path, monkeypatch):
    monkeypatch.setattr(sales, 'BATCH_SIZE', 1)
    monkeypatch.setattr(sales, 'sleep', lambda _: None)
    products = ['1234567890123456701', '1234567890123456702']
    book = Workbook(); sheet = book.active; sheet.append(sales.COLUMNS)
    row = {'产品ID': products[1], '集团': '集团二部', '商品ID': 'I2', '站点': 'PH',
           '店铺名称': 'SHOP', '本地主sku': 'MAIN', '本地子sku': 'SKU', '平台子sku': 'SKU'}
    sheet.append([row.get(column) for column in sales.COLUMNS])
    buffer = BytesIO(); book.save(buffer); book.close()
    monkeypatch.setattr(sales, 'MAX_DOWNLOAD_BYTES', len(buffer.getvalue()))
    submitted = []; downloads = []
    def route(request):
        path = request.url.path
        if path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'private', 'expires_in': 3600})
        if path == sales.EXPORT:
            submitted.append(json.loads(request.content)['productIdStr'])
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'id': str(len(submitted))}})
        if path == sales.TASKS:
            rows = [{'id': str(i), 'taskType': 1, 'status': 3,
                     'excelUrl': f'http://121.46.237.218:19110/{i}.xlsx'}
                    for i in range(1, len(submitted)+1)]
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': rows}})
        if path.endswith('.xlsx'):
            downloads.append(path)
            return httpx.Response(200, content=(b'x' * (len(buffer.getvalue())+1)
                                  if path == '/1.xlsx' else buffer.getvalue()))
        return httpx.Response(200, json=[])
    settings = Settings(_env_file=None, runtime_dir=tmp_path, vendor_username='u', vendor_password='p',
                        sales_request_interval_seconds=0)
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        result = sales.capture_sales_batch(settings, {'capture_id': 'capture', 'groups': []},
            {'O1': [products[0]], 'O2': [products[1]]},
            {key: {'erp_shop': 'SHOP', 'shop_id': 'SHOP-ID'} for key in ('O1','O2')}, client=client)
    assert submitted == products
    assert downloads == ['/1.xlsx', '/2.xlsx']
    assert result['summary']['variants'] == 1
    assert result['issues'][0]['reason'] == 'sales_export_file_too_large'
