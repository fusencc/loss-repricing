import pytest

from app.adapters.sales_batch_export import select_export_candidates


def test_select_export_candidates_deduplicates_and_rejects_mixed_margin():
    def row(order, amount, product):
        return {'orderid': order, 'fields': {'修正一次毛利': amount, 'item ID': product}}
    capture = {'groups': [
        {'candidate_order_ids': ['one', 'two', 'three'], 'rows': [
            row('one', '-1', '12345678901'), row('two', '1', '12345678902'),
            row('three', '-2', '12345678903')]},
        {'candidate_order_ids': ['one', 'three'], 'rows': [
            row('one', '-1', '12345678901'), row('three', '2', '12345678903')]},
    ]}
    assert select_export_candidates(capture, {'one', 'two', 'three'}) == {'one': ('12345678901',)}


def test_export_download_verifies_coverage_and_uses_original_headers(tmp_path):
    from io import BytesIO

    import httpx
    import pytest
    from openpyxl import Workbook

    from app.adapters.sales_batch_export import COLUMNS, EXPORT, TASKS, _export_file

    first, second = '1234567890123456701', '1234567890123456702'
    book = Workbook()
    sheet = book.active
    sheet.append(COLUMNS)
    for product in (first, second):
        row = {'产品ID': product, '集团': '集团二部', '商品ID': product + '0', 'VAT费率': 7.5}
        sheet.append([row.get(column) for column in COLUMNS])
    buffer = BytesIO()
    book.save(buffer)
    book.close()

    class Auth:
        def get_token(self):
            return 'test-token'

    calls = []
    def route(request):
        path = request.url.path
        calls.append(path)
        if path == EXPORT:
            import json
            assert json.loads(request.content)['productIdStr'].split()[0] == first
            return httpx.Response(200, json={'resp_code': 0, 'datas': None})
        if path == TASKS:
            data = [] if calls.count(TASKS) % 2 else [
                {'id': '1', 'taskType': 1, 'status': 3, 'excelUrl': 'http://121.46.237.218:19110/export.xlsx'}]
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': data}})
        assert 'Authorization' not in request.headers
        return httpx.Response(200, content=buffer.getvalue())

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        _, task_id, rows = _export_file(client, Auth(), [first, second], '集团二部')
        assert task_id == '1'
        assert [row['商品ID'] for _, row in rows] == [first + '0', second + '0']
        assert rows[0][1]['VAT费率'] == 7.5
        with pytest.raises(ValueError, match='sales_export_outside_request'):
            _export_file(client, Auth(), [first, '1234567890123456703'], '集团二部')


def test_erp_goods_sales_are_only_requested_after_official_history_passes(monkeypatch):
    from types import SimpleNamespace

    from app.services import erp_workflow

    def target(item_id):
        return {'key': {'platform': 'TikTok', 'shop_id': 'shop', 'site': 'PH',
                        'product_id': 'parent', 'item_id': item_id,
                        'sku': item_id + '-sku', 'warehouse': 'warehouse'}}

    protected, allowed = target('protected'), target('allowed')
    calls = []

    def recent(settings, keys):
        calls.append('official-history')
        assert [key.item_id for key in keys] == ['protected', 'allowed']
        return 'official-source'

    def reference(settings, capture, targets, **kwargs):
        calls.append('erp-sales')
        assert targets == [allowed]
        return 'sales-source'

    monkeypatch.setattr(erp_workflow, 'capture_recent_price_changes', recent)
    monkeypatch.setattr(erp_workflow, 'recent_price_log_for',
                        lambda _, key: SimpleNamespace(count=int(key.item_id == 'protected')))
    monkeypatch.setattr(erp_workflow, 'capture_goods_sales', reference)
    _, eligible, _, filtered = erp_workflow.capture_candidate_references({}, {}, [protected, allowed])
    assert calls == ['official-history', 'erp-sales']
    assert eligible == [allowed]
    assert [row['status'] for row in filtered] == ['history_protected', 'passed']


def test_batch_prices_are_used_only_when_current_platform_confirms_them():
    from app.adapters.live_online_preparation import prepare_online_states
    from test_live_online_preparation import sources

    capture, targets, inputs = sources()
    record = inputs['platform']['records'][0]
    record['company_activity_locked'] = False
    record['current_variants'][0]['marketPrice'] = '61.49'
    exported = {'币种': 'PHP', '含vat价格': '61.49', '营销价格': '', '折扣': '', '活动类型': '',
                '7天销量': '7', '海外仓公式利润率(%)': '-14.93'}
    inputs['sales'].update(source_mode='online_sku_batch_export', variants=[
        {'shopName': 'SHOPPH', 'productId': 'P1', 'sellerSkuId': 'I1',
         'batch_pricing': exported, 'export_ref': 'SALES/001.xlsx:2'}
    ])
    report = prepare_online_states(capture, targets, **inputs)
    assert report['online'][0]['state']['marketing_price'] == '61.49'
    assert report['online'][0]['state']['sales_7d'] == 0  # ERP 按仓销量，与导出参考列口径独立。
    exported['含vat价格'] = '60'
    report = prepare_online_states(capture, targets, **inputs)
    assert not report['online']
    assert report['issues'][0]['reason'] == '批量导出价格与当前平台快照不一致'


@pytest.mark.parametrize(('corrected_margin', 'selected'), [('-1', True), ('0', False), ('1', False), (None, False)])
def test_candidates_use_erp_corrected_first_margin_even_when_other_profit_fields_disagree(corrected_margin, selected):
    fields = {'修正一次毛利': corrected_margin, '一次毛利': '10' if selected else '-10',
              '毛利': '10' if selected else '-10', '修正毛利': '10' if selected else '-10',
              'item ID': '12345678901'}
    capture = {'groups': [{'rows': [{'orderid': 'O1', 'fields': fields}]}]}
    result = select_export_candidates(capture, {'O1'})
    assert result == ({'O1': ('12345678901',)} if selected else {})
