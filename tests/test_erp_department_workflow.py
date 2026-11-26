"""Department scheduling regressions with isolated ERP responses."""
import json
from pathlib import Path

import httpx
import pytest

from app.adapters import erp_bulk_capture as bulk
from app.config import Settings
from app.services import erp_workflow
from app.services.erp_pricing import preview_pricing
from scripts.verify_erp_replay import check_originals
from test_erp_bulk_capture import xlsx


def test_four_source_candidate_summary_keeps_shop_and_seller_boundaries():
    from app.services.erp_seller_candidates import summarize_seller_candidates

    groups = [
        {'source': 'loss_allocatable', 'candidate_order_ids': ['1', '2'],
         'loss_rows': [{'订单编号': '1', '店铺': 'A', '销售员': '甲'},
                       {'订单编号': '2', '店铺': 'A', '销售员': '甲'}], 'rows': []},
        {'source': 'loss_pending', 'candidate_order_ids': ['1', '2'],
         'loss_rows': [{'订单编号': '1', '店铺': 'B', '销售员': '乙'},
                       {'订单编号': '2', '店铺': 'A', '销售员': '乙'}], 'rows': []},
        {'source': 'blocked_allocatable', 'candidate_order_ids': ['1'],
         'blocked_shops': {'1': {'shopid': 'A1', 'shopname': 'A'}}, 'rows': []},
        {'source': 'blocked_pending', 'candidate_order_ids': ['3', '4'],
         'blocked_shops': {'3': {'shopid': 'B1', 'shopname': 'B'}},
         'rows': [{'orderid': '3', 'fields': {'店铺': 'B', '销售员': '乙'}}]},
    ]
    result = summarize_seller_candidates({'groups': groups}, {'orders': {}})
    assert result['sellers'] == {
        '甲': [{'shop_id': 'A1', 'order_id': '1', 'sources': ['loss_allocatable', 'blocked_allocatable']}],
        '乙': [{'shop_id': 'B1', 'order_id': '1', 'sources': ['loss_pending']},
              {'shop_id': 'B1', 'order_id': '3', 'sources': ['blocked_pending']}],
    }
    assert {row['order_id']: row['reason'] for row in result['unresolved']} == {
        '2': 'salesperson_conflict', '4': 'shop_unverified',
    }


@pytest.mark.parametrize('malformed_task_list', [False, True])
def test_department_finance_batches_follow_sources_without_seller_priority(tmp_path, malformed_task_list):
    department = 'Tiktok十二部'
    headers = ['订单编号', '订单状态', '平台', '集团', '部门', '销售员', '毛利', '修正毛利', '修正一次毛利']
    records = {
        'L1': ['L1', '可配货', 'Tiktok', '集团二部', department, '多单'],
        'L2': ['L2', '待处理', 'Tiktok', '集团二部', department, '少单'],
        'L3': ['L3', '待处理', 'Tiktok', '集团二部', department, '多单'],
        'B1': ['B1', '待处理', 'Tiktok', '集团二部', 'Tiktok五部', '其他'],
    }
    requests = []
    submitted = []

    def handler(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        requests.append(path)
        if path.endswith('/login'):
            return httpx.Response(200, json={'code': 200, 'data': {'token': 'test'}})
        if path == bulk.LOSS_EXPORT:
            assert body['department'] == department
            ids = ['L1'] if body['status'] == 4 else ['L2', 'L3']
            return httpx.Response(200, content=xlsx(headers, [records[order] + [-1, -1, -1] for order in ids]))
        if path == bulk.BLOCKED_LIST:
            assert body['departments'] == department
            ids = ['L1'] if body['status'] == 4 else ['B1']
            rows = [{'orderid': order, 'status': str(body['status']), 'jzfh': '1', 'platform': 'Tiktok'} for order in ids]
            return httpx.Response(200, json={'code': 0, 'data': {'records': rows, 'total': len(rows)}})
        if path == bulk.ORDER_EXPORT:
            submitted.append(body['orderIdList'])
            return httpx.Response(200, json={'code': 200, 'data': '下载任务【task】后台执行中'})
        if path == bulk.TASKS:
            task = {'id': 'task', 'fileName': 'task', 'source': 'orderList', 'status': 'success', 'downloadUrl': '/finance.xlsx'}
            tasks = [None] if malformed_task_list and submitted[-1] == ['L2', 'L3'] else [task]
            return httpx.Response(200, json={'code': 200, 'data': tasks})
        assert path == '/finance.xlsx'
        return httpx.Response(200, content=xlsx(headers, [records[order] + [-1, -1, -1] for order in submitted[-1]]))

    settings = Settings(_env_file=None, runtime_dir=tmp_path, erp_username='u', erp_password='p')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        capture = bulk.capture_erp_bulk(settings, client=client, department=department)

    assert submitted == [['L1'], ['L2', 'L3'], ['L1'], ['B1']]
    assert requests[:5] == ['/newIp/system/login', bulk.LOSS_EXPORT, bulk.LOSS_EXPORT, bulk.BLOCKED_LIST, bulk.BLOCKED_LIST]
    assert capture['department'] == department
    assert 'salesperson_priority' not in capture
    assert not capture['capture_complete']
    assert capture['groups'][-1]['error'] and not capture['groups'][-1]['rows']
    loss_pending = capture['groups'][1]
    assert bool(loss_pending['error']) is malformed_task_list
    assert [row['orderid'] for row in loss_pending['rows']] == ([] if malformed_task_list else ['L2', 'L3'])
    assert all(capture['groups'][index]['stage'] == 'complete' for index in (0, 2))
    assert check_originals(capture)['failures'] == []


def test_workflow_continues_after_incomplete_or_failed_department(tmp_path, monkeypatch):
    attempted = []
    sessions = []

    def collect(settings, *, web_session, **kwargs):
        assert 'department' not in kwargs
        sessions.append(web_session)
        return {'capture_id': 'group', 'capture_complete': True}

    def lookup(settings, capture, progress, session):
        sessions.append(session)
        attempted.append(('lookup', capture['department']))
        if capture['department'] == 'Tiktok五部':
            raise RuntimeError('synthetic departmental lookup failure')
        return None, None

    def evaluate(settings, capture, *, client, auth, **kwargs):
        attempted.append(('evaluate', capture['department']))
        clients.append((client, auth))
        if capture['capture_id'] == 'Tiktok一部':
            return {'status': 'blocked', 'ready_prices': 0}
        return {'status': 'completed', 'ready_prices': 1}

    clients = []
    monkeypatch.setattr(erp_workflow, 'refresh_daily_fees', lambda *a, **k: {})
    monkeypatch.setattr(erp_workflow, 'query_current_erp_fx', lambda *a: {'ref': 'test-fx'})
    monkeypatch.setattr(erp_workflow, 'capture_erp_bulk', collect)
    monkeypatch.setattr(erp_workflow, 'save_capture', lambda runtime, result: result)
    monkeypatch.setattr(erp_workflow, 'department_captures', lambda parent, depts: (
        {d: {'department': d, 'capture_id': d, 'capture_complete': True, 'cleaning_status': 'saved'} for d in depts}, {}))
    monkeypatch.setattr(erp_workflow, '_capture_erp_sources', lookup)
    monkeypatch.setattr(erp_workflow, '_evaluate_capture', evaluate)
    result = erp_workflow.run_workflow(Settings(_env_file=None, runtime_dir=tmp_path))
    assert attempted == ([('lookup', d) for d in bulk.ERP_DEPARTMENTS] +
                         [('evaluate', d) for d in bulk.ERP_DEPARTMENTS if d != 'Tiktok五部'])
    assert len(sessions) == 5
    assert [dept['status'] for dept in result['departments']] == ['blocked', 'failed', 'completed', 'completed']
    assert result['status'] == 'needs_attention'
    assert result['ready_prices'] == 2
    assert all(session is sessions[0] for session in sessions)
    assert all(client is clients[0][0] and auth is clients[0][1] for client, auth in clients)
    assert clients[0][0].is_closed
    assert not result['execution_allowed']
    assert json.loads((Path(result['artifact_dir']) / 'result.json').read_text()) == result


def test_clean_index_failure_does_not_block_saved_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(erp_workflow, 'refresh_daily_fees', lambda *args, **kwargs: {})
    monkeypatch.setattr(erp_workflow, 'query_current_erp_fx', lambda *a: {'ref': 'test-fx'})
    monkeypatch.setattr(erp_workflow, 'ERP_DEPARTMENTS', ('Tiktok一部',))
    monkeypatch.setattr(erp_workflow, 'capture_erp_bulk', lambda *args, **kwargs: {})
    monkeypatch.setattr(erp_workflow, 'save_capture', lambda *args: {
        'capture_id': 'already-saved', 'capture_complete': True, 'cleaning_status': 'failed', 'groups': [],
        'department': 'Tiktok一部',
    })

    result = erp_workflow.run_workflow(Settings(_env_file=None, runtime_dir=tmp_path))

    assert result['status'] == 'completed'
    department, = result['departments']
    assert department['capture_id'] == 'already-saved'
    assert department['cleaning_status'] == 'failed'


@pytest.mark.parametrize('bad_scope', [None, 'filter', 'finance_row', 'online'])
def test_department_archive_scope_is_enforced_by_pricing(bad_scope):
    fixtures = Path(__file__).parent / 'fixtures'
    capture = json.loads((fixtures / 'erp-pricing-capture.sample.json').read_text())
    evidence = json.loads((fixtures / 'erp-pricing-evidence.sample.json').read_text())
    department = 'Tiktok十二部'
    capture['department'] = department
    for group in capture['groups']:
        department_key = 'department' if group['source'].startswith('loss_') else 'departments'
        group['filters'][department_key] = department
        for row in group['rows']:
            row['fields']['部门'] = department
    for entry in evidence['online']:
        entry['state']['profit_check']['department'] = 'TIKTOK十二部' if bad_scope != 'online' else 'TIKTOK五部'
    if bad_scope == 'filter':
        capture['groups'][0]['filters']['department'] = 'Tiktok五部'
    if bad_scope == 'finance_row':
        capture['groups'][0]['rows'][0]['fields']['部门'] = 'Tiktok五部'

    report = preview_pricing(capture, evidence)

    assert report['consistent_capture_scope'] == (bad_scope != 'filter')
    if bad_scope in (None, 'online'):
        assert report['evaluation_complete_sources'] == list(bulk.SOURCES)
    if bad_scope is None:
        assert report['summary']['ready_prices'] >= 1
    elif bad_scope == 'online':
        assert report['summary']['ready_prices'] == 0
        assert any(issue['code'] == 'online_department_mismatch' for issue in report['issues'])
    elif bad_scope == 'finance_row':
        assert 'loss_allocatable' not in report['evaluation_complete_sources']
        assert report['summary']['ready_prices'] > 0
    else:
        assert 'loss_allocatable' not in report['evaluation_complete_sources']
        assert report['summary']['ready_prices'] == 0


def test_round_reuses_real_erp_and_sales_authentication_and_clock(tmp_path, monkeypatch):
    from app.adapters import erp_bulk_capture, sales_auth
    from app.adapters.official_price_logs import capture_recent_price_changes
    from app.adapters.sales_platform_capture import capture_platform_controls
    from app.domain.types import VariantKey
    from app.adapters.erp_order_headers import capture_order_headers

    erp_clock, sales_clock = [0], [0]
    def erp_sleep(seconds):
        erp_clock[0] += seconds
    def sales_sleep(seconds):
        sales_clock[0] += seconds
    monkeypatch.setattr(erp_bulk_capture, 'monotonic', lambda: erp_clock[0])
    monkeypatch.setattr(erp_bulk_capture, 'sleep', erp_sleep)
    monkeypatch.setattr(sales_auth, 'monotonic', lambda: sales_clock[0])
    monkeypatch.setattr(sales_auth, 'sleep', sales_sleep)
    erp_calls, sales_calls = [], []
    def erp_handler(request):
        erp_calls.append((request.url.path, erp_clock[0]))
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json={'code':200,'data':{'accessToken':'ERP-TEST'}})
        assert request.headers['authorization'] == 'ERP-TEST'
        body = json.loads(request.content)
        assert body['filter1'] == 'r.orderid'
        row = {'orderid':'ORDER','shopid':'SHOP','shopname':'SHOPPH','platform':'TikTok'}
        return httpx.Response(200, json={'code':0,'data':{'records':[row],'total':1}})

    def sales_handler(request):
        sales_calls.append((request.url.path, sales_clock[0]))
        if request.url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token':'SALES-TEST','expires_in':3600})
        assert request.headers['authorization'] == 'Bearer SALES-TEST'
        if request.url.path.endswith('/findByPage'):
            assert json.loads(request.content)['productIdStr'] == 'PRODUCT'
            return httpx.Response(200, json={'code':0,'count':0,'data':[]})
        return httpx.Response(200,json={'resp_code':0,'datas':[{'sellerSkuId':'ITEM','count':0}]})

    real_client = httpx.Client
    clients = [
        real_client(transport=httpx.MockTransport(sales_handler)),
        real_client(transport=httpx.MockTransport(erp_handler)),
    ]
    mock_clients = iter(clients)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: next(mock_clients))
    key = VariantKey(platform='TikTok',shop_id='SHOP',site='PH',product_id='PRODUCT',
                     item_id='ITEM',sku='SKU',warehouse='WAREHOUSE')

    def collect(settings, *, web_session, **kwargs):
        assert 'department' not in kwargs
        return {'capture_id': 'parent', 'capture_complete': True}

    monkeypatch.setattr(erp_workflow, 'department_captures', lambda parent, depts: ({
        d: {'capture_id': d, 'department': d, 'capture_complete': True, 'cleaning_status': 'saved',
            'groups': [{'candidate_order_ids': ['ORDER']}]} for d in depts}, {}))

    def evaluate(settings, capture, *, headers, client, auth, **kwargs):
        assert headers['acquisition_complete']
        assert capture_recent_price_changes(settings,[key],client=client,auth=auth)['acquisition_complete']
        platform = capture_platform_controls(settings,capture,
            [{'erp_shop':'SHOPPH','product_id':'PRODUCT','item_id':'ITEM'}],client=client,auth=auth)
        assert platform['issues'][0]['code'] == 'company_activity_state_conflicting_or_missing'
        return {'status':'completed','ready_prices':0}

    monkeypatch.setattr(erp_workflow, 'refresh_daily_fees', lambda *a, **k: {})
    monkeypatch.setattr(erp_workflow, 'query_current_erp_fx', lambda *a: {'ref': 'test-fx'})
    monkeypatch.setattr(erp_workflow, 'capture_erp_bulk', collect)
    monkeypatch.setattr(erp_workflow, 'save_capture', lambda runtime, result: result)
    monkeypatch.setattr(erp_workflow, 'capture_erp_details', lambda *a, **k: None)
    monkeypatch.setattr(erp_workflow, '_evaluate_capture', evaluate)
    settings = Settings(_env_file=None,runtime_dir=tmp_path,
                        erp_username='U',erp_password='P',vendor_username='U',vendor_password='P',
                        erp_capture_request_interval_seconds=16,sales_request_interval_seconds=2)
    result = erp_workflow.run_workflow(settings)
    assert result['status'] == 'completed'
    assert len(erp_calls) == 5 and sum(path.endswith('/login') for path, _ in erp_calls) == 1
    assert [when for _,when in erp_calls] == list(range(0, 5 * 16, 16))
    assert len(sales_calls) == 13 and sum(path.endswith('/token') for path, _ in sales_calls) == 1
    assert [when for _,when in sales_calls[1:]] == list(range(0, 24, 2))
    assert all(client.is_closed for client in clients)
