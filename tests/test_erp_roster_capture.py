"""名单人员按部门合采，只对范围内订单导出和派生个人归档。"""
import json

import httpx

from app.adapters.erp_bulk_capture import capture_erp_bulk
from app.adapters.erp_pricing import BulkCapture
from app.config import Settings
from app.services.erp_capture_store import load_capture, save_capture
from app.services.erp_roster_capture import project_seller_capture
from test_erp_bulk_capture import xlsx


def test_department_capture_batches_roster_finance_then_splits_by_seller(tmp_path):
    department = 'Tiktok一部'
    calls, submitted = [], []
    fields = ['订单编号', '订单状态', '平台', '集团', '部门', '销售员', '毛利', '修正毛利', '修正一次毛利']

    def row(order, seller, status='可配货'):
        return [order, status, 'TikTok', '集团二部', department, seller, -1, -1, -1]

    def handle(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        calls.append((path, body))
        if path.endswith('/login'):
            return httpx.Response(200, json={'code': 200, 'data': {'token': 'mock-token'}})
        if path.endswith('/dowDeficitOrder'):
            assert body['department'] == department and 'username' not in body
            rows = [row('A', '甲'), row('B', '乙'), row('CONFLICT', '甲'), row('OTHER', '丙')]
            return httpx.Response(200, content=xlsx(fields, rows if body['status'] == 4 else []))
        if path == '/Api/order/list':
            assert body['departments'] == department and 'shopUsernameid' not in body
            records = [{'orderid': key, 'shopUsername': seller, 'platform': 'TikTok', 'status': 3,
                        'jzfh': '1', 'shopid': 'SHOP', 'shopname': 'SHOPPH'}
                       for key, seller in [('C', '甲'), ('D', '乙'), ('CONFLICT', '乙'), ('OTHER', '丙')]]
            records = records if body['status'] == 3 else []
            return httpx.Response(200, json={'code': 0, 'data': {'total': len(records), 'records': records}})
        if path.endswith('/asyncOrderListDownload'):
            submitted.append(body['orderIdList'])
            return httpx.Response(200, json={'code': 200, 'data': f'下载任务【task-{len(submitted)}】后台执行中'})
        if path.endswith('/download/list'):
            return httpx.Response(200, json={'code': 200, 'data': [
                {'id': index, 'fileName': f'task-{index}', 'source': 'orderList', 'status': 'success',
                 'downloadUrl': f'/test-{index}.xlsx'} for index in range(1, len(submitted) + 1)]})
        if path == '/test-1.xlsx':
            # 财务归属转移后不将 B 转交给另一个人，A 正常保留。
            return httpx.Response(200, content=xlsx(fields, [row('A', '甲'), row('B', '甲')]))
        if path == '/test-2.xlsx':
            return httpx.Response(200, content=xlsx(fields, [row('C', '甲', '待处理'), row('D', '乙', '待处理')]))
        raise AssertionError(path)

    config = Settings(_env_file=None, runtime_dir=tmp_path, erp_username='mock', erp_password='mock')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = capture_erp_bulk(config, department=department, roster_salespeople=['甲', '乙'], client=client)
    assert len([path for path, _ in calls if path.endswith('/login')]) == 1
    assert len([path for path, _ in calls if path.endswith('/dowDeficitOrder')]) == 2
    assert len([path for path, _ in calls if path == '/Api/order/list']) == 2
    assert submitted == [['A', 'B'], ['C', 'D']]
    assert not list(tmp_path.rglob('*-loss-orders.xlsx'))
    assert result['groups'][0]['missing_order_ids'] == ['B']
    parent = save_capture(tmp_path, result)

    orders = ['A', 'B', 'C', 'D']
    source = {'source_id': 'combined-source', 'erp_capture_id': parent['capture_id'], 'synthetic': False,
              'sample_only': False, 'queried_at': parent['queried_at'], 'completed_at': parent['queried_at'],
              'artifact_file': str(tmp_path / 'department.json'), 'acquisition_complete': True}
    headers = {**source, 'schema_version': 'erp-order-headers-v1', 'orders': {key: {'orderid': key} for key in orders}}
    native = {**source, 'schema_version': 'erp-order-details-v1', 'batches': [{'order_ids': orders, 'status': 'complete'}],
              'rows': [{'order_id': key} for key in orders]}
    for name, expected, complete in [('甲', ['A', 'C'], True), ('乙', ['B', 'D'], False)]:
        capture, per_header, per_native = project_seller_capture(tmp_path, parent, headers, native, name)
        archive = load_capture(tmp_path, capture['capture_id'])
        modeled = BulkCapture.model_validate(archive)
        assert modeled.capture_complete is complete and modeled.salesperson.name == name
        assert [order for group in modeled.groups for order in group.candidate_order_ids] == expected
        assert modeled.roster_parent_capture_id == parent['capture_id']
        group = modeled.groups[0]
        assert group.finance_coverage(group.scope_bloc(department), department, modeled.salesperson) == (complete, not complete)
        for art in (per_header, per_native):
            assert art['erp_capture_id'] == capture['capture_id']
            assert art['requested_order_ids'] == expected
        assert [entry['order_id'] for entry in per_native['rows']] == expected
