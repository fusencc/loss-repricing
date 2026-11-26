"""合成数据对照：仅改变一处输入，核对独立结果是否仍可消费。"""
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
from openpyxl import Workbook

from app.adapters import sales_batch_export, sales_platform_capture
from app.services.erp_pricing import preview_pricing


def sample():
    fixtures = Path(__file__).parent / 'fixtures'
    return [json.loads((fixtures / f'erp-pricing-{name}.sample.json').read_text()) for name in ('capture', 'evidence')]


def ready(capture, evidence):
    return {d['key']['item_id'] for d in preview_pricing(capture, evidence)['decisions'] if d['ready']}


def test_single_bad_finance_shop_preserves_independent_prices():
    capture, evidence = sample()
    before = ready(capture, evidence)
    assert before == {'0001001', '0001002'}
    row = next(r for r in capture['groups'][1]['rows'] if r['orderid'] == '000003')
    row['fields']['店铺'] = 123
    report = preview_pricing(capture, evidence)
    assert any(i['order_id'] == '000003' and i['code'] == 'missing_order_shop' for i in report['issues'])
    assert {d['key']['item_id'] for d in report['decisions'] if d['ready']} == before


def test_blank_product_in_download_preserves_identified_row():
    def parse(products):
        book = Workbook()
        book.active.append(sales_batch_export.COLUMNS)
        for product in products:
            row = {'产品ID': product, '集团': 'G'}
            book.active.append([row.get(col) for col in sales_batch_export.COLUMNS])
        buf = BytesIO()
        book.save(buf)
        book.close()
        return [r['产品ID'] for _, r in sales_batch_export._parse_export(buf.getvalue(), {'P'}, 'G')]
    assert parse(['P']) == parse(['P', None]) == ['P']


def test_activity_count_drift_preserves_observed_rows(tmp_path):
    settings = SimpleNamespace(sales_request_interval_seconds=0, erp_capture_bloc='G')
    auth = SimpleNamespace(collection_headers=lambda: {}, observe_collection_response=lambda response: None,
                           stopped_http_status=None)
    plans = {('S', p): {'I'} for p in ('P1', 'P2')}
    def collect(drift):
        def route(request):
            active = json.loads(request.content)['isPromotion'] == 1
            rows = [{'bloc': 'G', 'productId': 'P1' if active else 'P2', 'shopName': 'S'}]
            return httpx.Response(200, json={'code': 0, 'count': len(rows) + int(active and drift), 'data': rows})
        with httpx.Client(transport=httpx.MockTransport(route)) as client:
            return {key for key, *_ in sales_platform_capture._confirmed_activity(
                settings, plans, client, auth, tmp_path, {'files': [], 'issues': []}, {})}
    assert collect(False) == collect(True) == plans.keys()


def test_invalid_price_does_not_discard_other_sku():
    capture, evidence = sample()
    before = ready(capture, evidence)
    assert before == {'0001001', '0001002'}
    state = evidence['online'][0]['state']
    state['retail_price'] = '0'
    assert ready(capture, evidence) == before - {state['key']['item_id']}
