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
from app.domain.engine import evaluate
from app.services.store import BEIJING
from fake_adapter import DemoAdapter


@pytest.mark.parametrize('discount,timeout,missing_off', [(0, False, False), (50, True, False), (50, False, True)])
def test_single_roster_upload_and_readback_use_proven_endpoints(tmp_path, monkeypatch, discount, timeout, missing_off):
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
        retail = decision.upload_price if sent[UPLOAD_PATH] else online.retail_price
        actual = retail * factor
        return {
            'acquisition_complete': True, 'source_id': 'verified-platform-now',
            'records': [{
                'observed_at': datetime.now(timezone.utc).isoformat(), 'company_activity_locked': bool(discount),
                'company_parent': {'shopName': shop, 'shopId': key.shop_id, 'productId': key.product_id,
                                   'localMainSku': online.main_sku, 'taxVatRate': '0'},
                'current_variants': [{
                    'shopName': shop, 'shopId': key.shop_id, 'productId': key.product_id,
                    'sellerSkuId': key.item_id, 'sellerSku': key.sku, 'currency': online.currency,
                    'warehousename': key.warehouse, 'overseasStockSum': '0',
                    'priceIncludeVat': str(retail), 'marketPrice': str(actual) if discount else None,
                    'discount': str(discount) if discount else None, 'logisticsCost': '0', 'promotionId': 'promotion',
                }],
                'data': {'id': key.product_id, 'skus': [{
                    'id': key.item_id, 'seller_sku': key.sku,
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
        if path.endswith('/operationLog/getRequestAndResponseById'):
            assert req.url.params['id'] == 'new-log'
            return httpx.Response(200, json={'resp_code': 0, 'datas': {
                'requestBody': {'skus': [{'id': key.item_id,
                                          'price': {'currency': online.currency, 'amount': price}}]},
                'responseResult': {'code': 0},
            }})
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
                        lambda *_args, **_kw: {'key': key.model_dump(mode='json'), 'shop_name': shop,
                            'latest_success_query': {'status': 'ok', 'amount': price}})
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
        assert results.actual_price == decision.upload_price * factor
        assert results.price_record['execution_id'] == execution_id
        if not discount:
            assert 'confirmed_no_activity' in results.price_record['prices']['marketing']['source_field']
        assert sent[UPLOAD_PATH] == 1 and sent[LOGIN_PATH] == 1

    workbook = load_workbook(tmp_path / 'erp-roster-price-executions' / execution_id / f'upload-{execution_id}.xlsx')
    try:
        sheet = workbook.active
        assert [sheet.cell(2, col).value for col in (1, 2, 3)] == [shop, key.product_id, key.item_id]
        assert Decimal(str(sheet['E2'].value)) == decision.upload_price
        assert all(sheet.cell(2, col).value is None for col in (4, 6, 7, 8, 9))
    finally:
        workbook.close()
