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
    waits = []

    def run_department(settings, *, department, progress):
        attempted.append(department)
        if department == 'Tiktok一部':
            return {'status': 'blocked', 'ready_prices': 0, 'stopped_by_rate_limit': True}
        if department == 'Tiktok五部':
            raise RuntimeError('synthetic departmental failure')
        return {'status': 'completed', 'ready_prices': 1}

    monkeypatch.setattr(erp_workflow, '_run_workflow', run_department, raising=False)
    monkeypatch.setattr(erp_workflow, 'sleep', waits.append)
    result = erp_workflow.run_workflow(Settings(_env_file=None, runtime_dir=tmp_path))
    assert attempted == ['Tiktok一部', 'Tiktok五部', 'Tiktok十二部', 'Tiktok二十二部']
    assert [dept['status'] for dept in result['departments']] == ['blocked', 'failed', 'completed', 'completed']
    assert result['status'] == 'needs_attention'
    assert result['ready_prices'] == 2
    assert waits == [16]
    assert not result['execution_allowed']
    assert json.loads((Path(result['artifact_dir']) / 'result.json').read_text()) == result


def test_failed_department_keeps_the_saved_capture_id(tmp_path, monkeypatch):
    monkeypatch.setattr(erp_workflow, 'ERP_DEPARTMENTS', ('Tiktok一部',))
    monkeypatch.setattr(erp_workflow, 'capture_erp_bulk', lambda *args, **kwargs: {})
    monkeypatch.setattr(erp_workflow, 'save_capture', lambda *args: {
        'capture_id': 'already-saved', 'capture_complete': True, 'cleaning_status': 'failed',
    })
    monkeypatch.setattr(erp_workflow, '_evaluate_capture', lambda *args, **kwargs: pytest.fail('清洗失败时不得算价'))

    result = erp_workflow.run_workflow(Settings(_env_file=None, runtime_dir=tmp_path))

    assert result['status'] == 'blocked'
    failed, = result['departments']
    assert failed['status'] == 'failed'
    assert failed['capture_id'] == 'already-saved'
    assert failed['cleaning_status'] == 'failed'


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
    else:
        assert 'loss_allocatable' not in report['evaluation_complete_sources']
        assert report['summary']['ready_prices'] == 0
