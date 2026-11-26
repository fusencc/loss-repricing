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
    from app.adapters.sales_auth import SalesAuth
    from app.config import Settings

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

    calls = []
    def route(request):
        path = request.url.path
        calls.append(path)
        if path.endswith('/token'):
            return httpx.Response(200, json={'access_token':'test-token','expires_in':3600})
        if path == EXPORT:
            import json
            body = json.loads(request.content)
            assert body['productIdStr'].split()[0] == first
            assert {'折扣', '海外仓库存', 'erp成本价', '包装重量'} <= set(body['exportField'])
            return httpx.Response(200, json={'resp_code': 0, 'datas': None})
        if path == TASKS:
            data = [] if calls.count(TASKS) % 2 else [
                {'id': '1', 'taskType': 1, 'status': 3, 'excelUrl': 'http://121.46.237.218:19110/export.xlsx'}]
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': data}})
        assert 'Authorization' not in request.headers
        return httpx.Response(200, content=buffer.getvalue())

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        auth = SalesAuth(Settings(_env_file=None,vendor_username='U',vendor_password='P',
                                  sales_request_interval_seconds=0),client)
        _, task_id, rows = _export_file(client, auth, [first, second], '集团二部')
        assert task_id == '1'
        assert [row['商品ID'] for _, row in rows] == [first + '0', second + '0']
        assert rows[0][1]['VAT费率'] == 7.5
        with pytest.raises(ValueError, match='sales_export_outside_request'):
            _export_file(client, auth, [first, '1234567890123456703'], '集团二部')


def test_partial_export_keeps_variants_and_reports_only_missing_shop_product_pairs(tmp_path):
    from io import BytesIO

    import httpx
    from openpyxl import Workbook

    from app.adapters import sales_batch_export as sales
    from app.config import Settings

    found, missing = '1234567890123456701', '1234567890123456702'
    book = Workbook()
    sheet = book.active
    sheet.append(sales.COLUMNS)
    sheet.append(['集团二部' if col == '集团' else None for col in sales.COLUMNS])
    for sku in ('A', 'B'):
        row = {'集团': '集团二部', '店铺名称': 'SHOPPH', '站点': 'PH', '产品ID': found,
               '本地主sku': 'MAIN', '本地子sku': sku, '平台子sku': sku, '商品ID': 'I' + sku}
        sheet.append([row.get(column) for column in sales.COLUMNS])
    buffer = BytesIO()
    book.save(buffer)
    book.close()

    def route(request):
        path = request.url.path
        if path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'test', 'expires_in': 3600})
        if path == sales.EXPORT:
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'id': '1'}})
        if path == sales.TASKS:
            tasks = [{'id': '1', 'taskType': 1, 'status': 3, 'excelUrl': 'http://121.46.237.218:19110/export.xlsx'}]
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': tasks}})
        if path == sales.CURRENCY_RATES:
            return httpx.Response(200, json=[])
        assert path == '/export.xlsx'
        return httpx.Response(200, content=buffer.getvalue())

    settings = Settings(_env_file=None, runtime_dir=tmp_path, vendor_username='u', vendor_password='p',
                        sales_request_interval_seconds=0)
    candidates = {'O1': [found], 'O2': [missing], 'O3': [found]}
    scopes = {order: {'erp_shop': shop, 'shop_id': shop + '-id'} for order, shop in
              [('O1', 'SHOPPH'), ('O2', 'SHOPPH'), ('O3', 'OTHERPH')]}
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        result = sales.capture_sales_batch(settings, {'capture_id': 'test', 'groups': []}, candidates, scopes, client=client)
    assert [(v['shopName'], v['productId'], v['localSellerSku'], v['sellerSkuId']) for v in result['variants']] == [
        ('SHOPPH', found, 'A', 'IA'), ('SHOPPH', found, 'B', 'IB')]
    assert not result['acquisition_complete']
    assert result['issues'] == [
        {'code': 'sales_export_shop_product_missing', 'product_id': found, 'shop': 'OTHERPH'},
        {'code': 'sales_export_shop_product_missing', 'product_id': missing, 'shop': 'SHOPPH'}]


def test_official_history_filters_candidates_before_online_preparation(monkeypatch):
    from types import SimpleNamespace

    from app.services import erp_workflow

    def target(item_id):
        return {'key': {'platform': 'TikTok', 'shop_id': 'shop', 'site': 'PH',
                        'product_id': 'parent', 'item_id': item_id,
                        'sku': item_id + '-sku', 'warehouse': 'warehouse'}}

    protected, allowed = target('protected'), target('allowed')
    calls = []

    def recent(settings, keys, **kwargs):
        calls.append('official-history')
        assert [key.item_id for key in keys] == ['protected', 'allowed']
        return 'official-source'

    monkeypatch.setattr(erp_workflow, 'capture_recent_price_changes', recent)
    monkeypatch.setattr(erp_workflow, 'recent_price_log_for',
                        lambda _, key: SimpleNamespace(count=int(key.item_id == 'protected')))
    source, eligible, filtered = erp_workflow.filter_candidate_history({}, [protected, allowed])
    assert source == 'official-source'
    assert calls == ['official-history']
    assert eligible == [allowed]
    assert [row['status'] for row in filtered] == ['history_protected', 'passed']


def test_batch_prices_are_used_only_when_current_platform_confirms_them():
    from app.adapters.live_online_preparation import prepare_online_states
    from test_live_online_preparation import sources

    capture, targets, inputs = sources()
    record = inputs['platform']['records'][0]
    record['company_activity_locked'] = False
    record['current_variants'][0]['marketPrice'] = None
    exported = {'币种': 'PHP', '含vat价格': '61.49', '营销价格': '', '折扣': '', '活动类型': '',
                '7天销量': '7', '海外仓公式利润率(%)': '-14.93', '海外仓库存': '114'}
    inputs['sales'].update(source_mode='online_sku_batch_export', variants=[
        {'shopName': 'SHOPPH', 'productId': 'P1', 'sellerSkuId': 'I1',
         'batch_pricing': exported, 'export_ref': 'SALES/001.xlsx:2'}
    ])
    report = prepare_online_states(capture, targets, **inputs)
    assert report['online'][0]['state']['marketing_price'] == '61.49'
    assert not report['issues']
    exported['含vat价格'] = '60'
    report = prepare_online_states(capture, targets, **inputs)
    assert not report['online']
    assert report['issues'][0]['reason'] == '批量导出价格与当前平台快照不一致'


@pytest.mark.parametrize(("off", "compatible"), [("50", True), ("90", False)])
def test_batch_discount_matches_current_marketing_price_at_display_precision(off, compatible):
    from decimal import Decimal

    from app.adapters.live_online_preparation import prepare_online_states
    from app.domain.money import MoneyRatio
    from app.domain.types import OnlineState
    from test_live_online_preparation import sources

    capture, targets, inputs = sources()
    inputs['platform']['records'][0]['current_variants'][0]['marketPrice'] = '30.75'
    inputs['sales'].update(source_mode='online_sku_batch_export', variants=[
        {'shopName': 'SHOPPH', 'productId': 'P1', 'sellerSkuId': 'I1', 'export_ref': 'SALES/001.xlsx:2',
         'batch_pricing': {'币种': 'PHP', '含vat价格': '61.49', '营销价格': '30.75',
                           '折扣': off, '活动类型': '直接折扣', '海外仓库存': '114'}}
    ])
    result = prepare_online_states(capture, targets, **inputs)
    if compatible:
        state = OnlineState.model_validate(result['online'][0]['state'])
        assert state.actual_price_and_discount() == (Decimal('30.75'), MoneyRatio(Decimal('0.5')))
    else:
        assert not result['online']
        assert result['issues'][0]['reason'] == '批量导出营销价与 OFF 折扣不一致'


def test_direct_profit_uses_exported_erp_cost_and_grams():
    from decimal import Decimal

    from app.adapters.live_online_preparation import prepare_online_states
    from test_live_online_preparation import configure_direct, sources
    from test_sales_fee_inputs import direct_source

    capture, targets, inputs = sources()
    _, rate = direct_source()
    configure_direct(inputs['fixed_fees']['config'], rate, 'PH')
    inputs['sales']['currency_rates'][1]['rate'] = '0.1'
    variant = inputs['platform']['records'][0]['current_variants'][0]
    variant.update(overseasStockSum=0, overseasStockWarehouseInfo='[]', warehousename='WH-PH',
                   localWeight=None, costPrice=None)
    inputs['sales'].update(source_mode='online_sku_batch_export', variants=[
        {'shopName': 'SHOPPH', 'productId': 'P1', 'sellerSkuId': 'I1', 'export_ref': 'SALES/001.xlsx:2',
         'batch_pricing': {'币种': 'PHP', '含vat价格': '61.49', '营销价格': '30.745',
                           '折扣': '50', '活动类型': '直接折扣', '海外仓库存': '0',
                           'erp成本价': '2', '包装重量': '21'}}
    ])
    result = prepare_online_states(capture, targets, **inputs)
    assert not result['issues']
    state = result['online'][0]['state']
    assert state['profit_check']['mode'] == 'direct'
    assert Decimal(state['profit_check']['model']['fixed_cost']) == Decimal('5')


@pytest.mark.parametrize(('corrected_margin', 'selected'), [('-1', True), ('0', False), ('1', False), (None, False)])
def test_candidates_use_erp_corrected_first_margin_even_when_other_profit_fields_disagree(corrected_margin, selected):
    fields = {'修正一次毛利': corrected_margin, '一次毛利': '10' if selected else '-10',
              '毛利': '10' if selected else '-10', '修正毛利': '10' if selected else '-10',
              'item ID': '12345678901'}
    capture = {'groups': [{'rows': [{'orderid': 'O1', 'fields': fields}]}]}
    result = select_export_candidates(capture, {'O1'})
    assert result == ({'O1': ('12345678901',)} if selected else {})
