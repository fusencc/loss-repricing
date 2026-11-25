import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from openpyxl import load_workbook

from app.adapters.http import UPLOAD_PATH
from app.adapters.roster_price_upload import ROSTER_UPLOAD_TRACE, RosterPriceTransport
from app.adapters.sales_auth import LOGIN_PATH
from app.config import Settings
from app.domain.engine import evaluate, SITE_CURRENCIES
from app.services.store import BEIJING
from fake_adapter import DemoAdapter


@pytest.mark.parametrize('discount,timeout,missing_off', [(0, False, False), (50, True, False), (50, False, True)])
@pytest.mark.parametrize('price_tail', ['0', '0.01'])
def test_roster_lookup_confirms_task_without_reading_prices(tmp_path, monkeypatch, discount, timeout, missing_off, price_tail):
    day = datetime.now(timezone.utc).astimezone(BEIJING).date()
    snapshot = DemoAdapter(tmp_path).collect(day)
    online = snapshot.online[0]
    online.discount_off_percent = None if missing_off else Decimal(discount)
    factor = (Decimal(100) - Decimal(discount)) / Decimal(100)
    online.marketing_price = online.retail_price * factor
    key = online.key
    decision = next(row for row in evaluate(snapshot, snapshot.as_of) if row.key == key)
    assert decision.ready
    price = str(decision.upload_price)
    sent = Counter()
    shop = 'ERP测试店'
    log = {'id': 'new-log', 'type': 2, 'status': 2, 'shopName': shop, 'productId': key.product_id,
           'sellerSkuId': key.item_id, 'priceIncludeVat': price, 'priceNew': price}

    def source(*args, **kwargs):
        assert not sent[UPLOAD_PATH], 'Post-upload lookups must not query current prices'
        retail = online.retail_price + Decimal(price_tail)
        actual = retail * factor
        return {
            'acquisition_complete': True, 'source_id': 'verified-platform-now',
            'records': [{
                'observed_at': datetime.now(timezone.utc).isoformat(), 'company_activity_locked': bool(discount),
                'company_parent': {'shopName': shop, 'shopId': key.shop_id, 'productId': key.product_id,
                                   'localMainSku': online.main_sku, 'taxVatRate': '0', 'productStatus': 0},
                'current_variants': [{
                    'shopName': shop, 'shopId': key.shop_id, 'productId': key.product_id,
                    'sellerSkuId': key.item_id, 'sellerSku': key.sku, 'currency': online.currency,
                    'warehousename': key.warehouse, 'overseasStockSum': '0',
                    'priceIncludeVat': str(retail), 'marketPrice': str(actual) if discount else None,
                    'discount': str(discount) if discount else None, 'logisticsCost': '0', 'promotionId': 'promotion',
                }],
                'data': {'id': key.product_id, 'product_status': 'ACTIVATE', 'status': 'ACTIVATE',
                         'is_not_for_sale': False, 'skus': [{
                    'id': key.item_id, 'seller_sku': key.sku, 'status_info': {'status': 'NORMAL'},
                    'price': {'currency': online.currency, 'sale_price': str(retail),
                              'tax_exclusive_price': str(retail)},
                }]},
            }],
        }

    def request(req):
        path = req.url.path
        sent[path] += 1
        if path == LOGIN_PATH:
            return httpx.Response(200, json={'access_token': 'fake-token', 'expires_in': 600})
        assert req.headers['authorization'] == 'Bearer fake-token'
        if path == UPLOAD_PATH:
            assert req.headers['content-type'].startswith('multipart/form-data')
            assert b'name="file"; filename="price.xlsx"' in req.content
            if timeout:
                raise httpx.ReadTimeout('simulated response loss', request=req)
            return httpx.Response(200, json={'resp_code': 0, 'resp_msg': '上传完成,'})
        if path.endswith('/operationLog/findByPage'):
            rows = [log] if sent[UPLOAD_PATH] else []
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'count': len(rows), 'data': rows}})
        if path.endswith('/promotion/findByPage'):
            row = {'shopid': key.shop_id, 'promotionId': 'promotion', 'promotionType': 2, 'productType': 2, 'status': 2}
            return httpx.Response(200, json={'code': 0, 'count': 1, 'data': [row]})
        if path.endswith('/promotion/productList'):
            row = {'shopid': key.shop_id, 'productId': key.product_id, 'sellerSkuId': key.item_id,
                   'promotionPrice': None, 'discount': str(discount),
                   'priceIncludeVat': str(online.retail_price), 'marketPrice': str(online.marketing_price)}
            return httpx.Response(200, json={'code': 0, 'count': 1, 'data': [row]})
        pytest.fail(f'unexpected sales endpoint {path}')

    monkeypatch.setattr('app.adapters.roster_price_upload.capture_platform_controls', source)
    monkeypatch.setattr('app.adapters.manual_live.capture_platform_controls', source)
    monkeypatch.setattr('app.adapters.official_price_logs.capture_official_price_logs',
                        lambda *_args, **_kw: pytest.fail('Unexpected official stats lookup'))
    config = Settings(_env_file=None, runtime_dir=tmp_path, live_submit_enabled=True,
                      vendor_username='test-vendor', vendor_password='test-only')
    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        transport = RosterPriceTransport(config, client=client)
        execution_id = str(uuid4())
        decision.trace[ROSTER_UPLOAD_TRACE] = transport.prepare(execution_id, 'batch', day, shop, decision)
        assert not sent[UPLOAD_PATH]
        result = transport.submit(execution_id, decision)
        assert result.status == ('unknown' if timeout else 'accepted')
        assert transport.submit(execution_id, decision).status == 'unknown'
        results = transport.lookup(execution_id, None, decision)
        assert results.status == 'accepted'
        assert results.actual_price is None and results.observed_at is None and results.price_record is None
        assert sent[UPLOAD_PATH] == 1 and sent[LOGIN_PATH] == 1

    workbook = load_workbook(tmp_path / 'erp-roster-price-executions' / execution_id / f'upload-{execution_id}.xlsx')
    try:
        sheet = workbook.active
        assert [sheet.cell(2, col).value for col in (1, 2, 3)] == [shop, key.product_id, key.item_id]
        assert Decimal(str(sheet['E2'].value)) == decision.upload_price
        assert all(sheet.cell(2, col).value is None for col in (4, 6, 7, 8, 9))
    finally:
        workbook.close()


@pytest.mark.parametrize('rows,task_id,expected', [
    ([{'id': 'old'}], None, 'unknown'),
    ([{'id': None}], None, 'unknown'),
    ([{'id': 'first'}, {'id': 'second'}], None, 'unknown'),
    ([{'id': 'first'}, {'id': 'second'}], 'second', 'accepted'),
    ([{}], 'different-id', 'unknown'),
    ([{'sellerSkuId': 'other'}], None, 'unknown'),
    ([{'productId': 'other'}], None, 'unknown'),
    ([{'shopName': 'other'}], None, 'unknown'),
    ([{'type': 1}], None, 'unknown'),
    ([{'priceIncludeVat': '999'}], None, 'unknown'),
    ([{'status': 1}], None, 'unknown'),
    ([{'status': 3}], None, 'rejected'),
    ([{'status': 5}], None, 'rejected'),
    ([{'status': 6}], None, 'rejected'),
    ([], None, 'unknown'),
])
def test_roster_task_requires_unique_new_bound_outcome(tmp_path, monkeypatch, rows, task_id, expected):
    from app.adapters.manual_live import ManualLiveAdapter

    snapshot = DemoAdapter(tmp_path).collect(datetime.now(BEIJING).date())
    decision = next(d for d in evaluate(snapshot, snapshot.as_of) if d.ready)
    key = decision.key
    a = {'key': key.model_dump(mode='json'), 'shop_name': '测试店', 'upload_price': str(decision.upload_price)}
    decision.trace[ROSTER_UPLOAD_TRACE] = {'authorization': a}
    execution_id = str(uuid4())
    directory = tmp_path / 'erp-roster-price-executions' / execution_id
    directory.mkdir(parents=True)
    (directory / f'intent-{execution_id}.json').write_text(json.dumps({'authorization': a, 'previous_log_ids': ['old']}))
    log = {'id': 'new', 'type': 2, 'status': 2, 'shopName': a['shop_name'], 'productId': key.product_id,
           'sellerSkuId': key.item_id, 'priceIncludeVat': a['upload_price']}
    monkeypatch.setattr(ManualLiveAdapter, '_logs', lambda self: [log | row for row in rows])
    with httpx.Client(transport=httpx.MockTransport(lambda req: pytest.fail(f'Unexpected request: {req.url.path}'))) as client:
        transport = RosterPriceTransport(Settings(_env_file=None, runtime_dir=tmp_path), client=client)
        result = transport.lookup(execution_id, task_id, decision)
        assert result.status == expected
        assert result.actual_price is None and result.price_record is None


@pytest.mark.parametrize('site', ['MY', 'PH', 'SG', 'TH', 'VN'])
@pytest.mark.parametrize('fulfillment', ['direct', 'overseas'])
def test_all_configured_site_fulfillments_are_enabled(site, fulfillment):
    from app.adapters.roster_price_upload import VERIFIED_FULFILLMENT
    assert SITE_CURRENCIES[site]
    assert fulfillment in VERIFIED_FULFILLMENT[site]
