"""部门单商品上传及价格回读；所有外部端点仅通过模拟传输访问。"""
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from uuid import uuid4

import httpx
import pytest
from openpyxl import load_workbook

from app.adapters.base import SourceUnavailable
from app.adapters.erp_price_upload import ERP_UPLOAD_TRACE, ErpPriceTransport
from app.adapters.http import UPLOAD_PATH
from app.adapters.sales_auth import LOGIN_PATH
from app.config import Settings
from app.domain.engine import evaluate
from app.services.store import BEIJING
from fake_adapter import DemoAdapter


@pytest.mark.parametrize('response_lost,readback,expected_status', [
    (False, 'live_price', 'accepted'), (True, 'live_price', 'accepted'),
    (False, 'wrong_sku', 'unknown'), (True, 'no_new_task', 'unknown'),
    (False, 'pending_task', 'unknown'), (False, 'failed_task', 'rejected'),
    (False, 'changed_retail', 'blocked'), (False, 'logs_unavailable', 'blocked'),
])
def test_single_department_upload_and_live_price_lookup(tmp_path, monkeypatch, response_lost, readback, expected_status):
    now = datetime.now(timezone.utc)
    day = now.astimezone(BEIJING).date()
    snapshot = DemoAdapter(tmp_path).collect(day)
    online = snapshot.online[0]
    online.discount_off_percent = Decimal(0)
    online.marketing_price = online.retail_price
    key = online.key
    decision = next(row for row in evaluate(snapshot, snapshot.as_of) if row.ready and row.key == key)
    shop = '部门测试店'
    sent = []
    live_reads = []
    log_queries = []
    live_retail = decision.upload_price + Decimal(1)
    live_actual = (live_retail * Decimal('.90')).quantize(Decimal('.01'))
    assert live_actual != decision.upload_price
    task = {'id': 'new', 'type': 2, 'status': 2, 'shopName': shop, 'productId': key.product_id,
            'sellerSkuId': key.item_id, 'priceIncludeVat': str(decision.upload_price)}

    def current(_cfg, source, targets, **kwargs):
        assert targets == [{'erp_shop': shop, 'product_id': key.product_id, 'item_id': key.item_id}]
        live_reads.append(source['capture_id'])
        retail = live_retail if sent or readback == 'changed_retail' else online.retail_price
        sku_text = 'OTHER' if sent and readback == 'wrong_sku' else key.sku
        return {'records': [{
            'company_parent': {'shopName': shop, 'shopId': key.shop_id, 'productId': key.product_id,
                               'localMainSku': online.main_sku},
            'data': {'id': key.product_id, 'skus': [{'id': key.item_id, 'seller_sku': sku_text,
                     'price': {'sale_price': str(retail), 'currency': online.currency}}]},
            'current_variants': [{'shopName': shop, 'shopId': key.shop_id, 'productId': key.product_id,
                                  'sellerSkuId': key.item_id, 'sellerSku': sku_text, 'currency': online.currency,
                                  'overseasStockSum': '0', 'priceIncludeVat': str(retail),
                                  'discount': '10' if sent else None, 'marketPrice': str(live_actual) if sent else None}],
            'company_activity_locked': bool(sent), 'variants_observed_at': now.isoformat()}]}

    def request(req):
        if req.url.path == LOGIN_PATH:
            return httpx.Response(200, json={'access_token': 'fake', 'expires_in': 600})
        if req.url.path == UPLOAD_PATH:
            sent.append(req)
            if response_lost:
                raise httpx.ReadTimeout('mock upload response lost', request=req)
            return httpx.Response(200, json={'resp_code': 0, 'resp_msg': '上传完成,'})
        if req.url.path.endswith('/operationLog/findByPage'):
            if readback == 'logs_unavailable':
                raise httpx.ReadTimeout('mock log read timeout', request=req)
            log_queries.append(json.loads(req.content))
            rows = [task | {'id': 'old'}]
            if sent and readback != 'no_new_task':
                status = {'pending_task': 1, 'failed_task': 3}.get(readback, 2)
                rows.append(task | {'status': status})
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'count': len(rows), 'data': rows}})
        pytest.fail(f'Unexpected endpoint {req.url.path}')

    monkeypatch.setattr('app.adapters.erp_price_upload.capture_platform_controls', current)
    config = Settings(_env_file=None, runtime_dir=tmp_path, live_submit_enabled=True,
                      vendor_username='mock', vendor_password='mock')
    execution_id = str(uuid4())
    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        transport = ErpPriceTransport(config, client)
        if expected_status == 'blocked':
            expected_error = SourceUnavailable if readback == 'logs_unavailable' else ValueError
            with pytest.raises(expected_error):
                transport.prepare(execution_id, 'batch', day, shop, decision)
            assert not sent
            return
        proof = transport.prepare(execution_id, 'batch', day, shop, decision)
        assert proof['previous_log_ids'] == ['old'] and not sent
        decision.trace[ERP_UPLOAD_TRACE] = proof
        if not response_lost and readback == 'live_price':
            from app.adapters.excel import price_upload_workbook
            simultaneous = Barrier(2)

            def build(*args):
                simultaneous.wait(timeout=5)
                return price_upload_workbook(*args)

            monkeypatch.setattr('app.adapters.manual_live.price_upload_workbook', build)
            with ThreadPoolExecutor(2) as pool:
                calls = [pool.submit(transport.submit, execution_id, decision) for _ in range(2)]
                assert sorted(call.result().status for call in calls) == ['accepted', 'unknown']
        else:
            result = transport.submit(execution_id, decision)
            assert result.status == ('unknown' if response_lost else 'accepted')
        assert transport.submit(execution_id, decision).status == 'unknown'
        restored = ErpPriceTransport(config, client)
        tomorrow = now + timedelta(days=1)
        monkeypatch.setattr('app.adapters.erp_price_upload.utcnow', lambda: tomorrow)
        task_id = None if response_lost else 'new'
        outcome = restored.lookup(execution_id, task_id, decision)
        end_day = tomorrow.astimezone(BEIJING).date() if task_id else day
        assert log_queries[-1]['createTimeEnd'] == f'{end_day} 23:59:59'
        assert outcome.status == expected_status
        if expected_status == 'accepted':
            assert outcome.actual_price == live_actual and outcome.observed_at == now
            assert outcome.price_basis == 'actual_including_vat'
        else:
            assert outcome.actual_price is None
        assert len(live_reads) == (2 if readback in {'live_price', 'wrong_sku'} else 1)
    assert len(sent) == 1
    headers = f"Content-Type: {sent[0].headers['content-type']}\r\n\r\n".encode()
    body = BytesParser(policy=policy.default).parsebytes(headers + sent[0].content)
    book = load_workbook(io.BytesIO(next(body.iter_parts()).get_payload(decode=True)))
    try:
        values = [cell.value for cell in book.active[2]]
        assert values[:4] == [shop, key.product_id, key.item_id, None]
        assert Decimal(str(values[4])) == decision.upload_price
        assert values[5:] == [None] * 4
    finally:
        book.close()
