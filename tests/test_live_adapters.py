from datetime import date, datetime
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from io import BytesIO
import json
from pathlib import Path

import httpx
from openpyxl import load_workbook
import pytest

from app.adapters.base import SourceUnavailable
from app.config import Settings
from app.domain.types import Decision, VariantKey


DAY = date(2026, 11, 5)
OBSERVED = "2026-11-05T09:00:00+08:00"
SOURCES = ["loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"]
IDENTITY = {
    "platform": "TikTok", "shop_id": "001", "site": "MY", "product_id": "002",
    "item_id": "003", "sku": "004", "warehouse": "direct",
}
KEY_MAP = {
    "key.platform": "product.platform", "key.shop_id": "product.shop", "key.site": "product.market",
    "key.product_id": "product.pid", "key.item_id": "product.item", "key.sku": "product.sku",
    "key.warehouse": "product.warehouse",
}
PRODUCT = {
    "platform": "TikTok", "shop": "001", "market": "MY", "pid": "002", "item": "003",
    "sku": "004", "warehouse": "direct",
}
ROW = {
    "product": PRODUCT, "detail": "0001", "order": "0002", "main": "0003",
    "qty": "3", "loss": "-0.9999999999999999", "ccy": "MYR",
}
ONLINE = {
    "product": PRODUCT, "main": "0003", "ccy": "MYR", "r": "20", "m": "10",
    "margin": "-0.04", "seen": OBSERVED, "qty": 10,
    "start": "2026-10-29", "end": "2026-11-04", "full": True, "history": True,
    "last": None, "pending": False, "protected": False, "locked": False, "sellable": True,
    "fee": {"factor": "0.91", "cost": "9.8", "rev": "fees-v1", "reviewed": True},
}


@pytest.fixture
def contract():
    row_fields = KEY_MAP | {
        "row_id": "detail", "order_id": "order", "main_sku": "main", "quantity": "qty",
        "loss_amount": "loss", "currency": "ccy",
    }
    online_fields = KEY_MAP | {
        "main_sku": "main", "currency": "ccy", "retail_price": "r", "marketing_price": "m",
        "current_margin": "margin", "observed_at": "seen", "sales_7d": "qty", "sales_start": "start",
        "sales_end": "end", "sales_complete": "full", "history_known": "history",
        "last_increase_at": "last", "pending_submission": "pending", "protected": "protected",
        "activity_locked": "locked", "sellable": "sellable", "fee_model.revenue_factor": "fee.factor",
        "fee_model.fixed_cost": "fee.cost", "fee_model.version": "fee.rev",
        "fee_model.verified": "fee.reviewed",
    }
    collection = {
        "version": "candidate-v1", "service": "erp", "method": "POST", "records_path": "payload.rows",
        "as_of_path": "payload.timestamp", "business_date_path": "payload.date", "field_map": row_fields,
        "constants": {"basis": "line", "fee_scope": "normal", "abnormal": False, "allocation_reliable": True},
    }
    return {
        "verified": True,
        "services": {"erp": {"base_url": "https://erp.example.test"}, "vendor": {"base_url": "https://sales.example.test"}},
        "collections": {
            **{source: collection | {"path": f"/candidates/{source}", "json": {"day": "{business_date}"}} for source in SOURCES},
            "online": collection | {
                "version": "online-v1", "service": "vendor", "method": "GET", "path": "/online",
                "params": {"day": "{business_date}", "from": "{sales_start}", "to": "{sales_end}"},
                "field_map": online_fields, "constants": {"fee_scope": "normal"},
            },
            "fx": {
                "version": "rates-v1", "service": "erp", "method": "GET", "path": "/fx",
                "params": {"day": "{business_date}"}, "as_of_path": "payload.timestamp",
                "business_date_path": "payload.date",
                "field_map": {"fx_date": "payload.rate_day", "rates": "payload.rates"},
            },
        },
        "sites": {
            "MY": {"verified": True, "price_step": "0.01", "rounding": "ROUND_HALF_UP",
                   "upload_column": "直发仓税后价格", "version": "my-v1", "fee_model_version": "fees-v1",
                   "warehouses": ["direct"]},
        },
        "submit": {
            "verified": True, "version": "upload-v1", "shop_labels": {"001": "模板专用店名"},
            "single_data_row_number": 2, "task_id_path": "datas.job", "execution_id_header": "X-Test-Run",
        },
        "lookup": {
            "verified": True, "version": "lookup-v1", "service": "vendor", "method": "POST", "path": "/actual",
            "max_age_seconds": 300,  # 合成测试合同，不是公司已确认阈值。
            "json": {
                "shop": "{shop_id}", "pid": "{product_id}", "item": "{item_id}", "sku": "{sku}",
                "site": "{site}", "platform": "{platform}", "warehouse": "{warehouse}",
                "run": "{execution_id}", "job": "{task_id}",
            },
            "field_map": KEY_MAP | {
                "actual_price": "value", "currency": "ccy", "observed_at": "readTime", "effective_at": "effectiveTime",
                "price_basis": "basis", "row_status": "state", "task_id": "job",
            },
            "rejected_status": "denied",
        },
    }


@pytest.fixture
def make_adapter(tmp_path, monkeypatch):
    from app.adapters.http import HttpAdapter

    monkeypatch.setattr("app.adapters.http._utcnow", lambda: datetime.fromisoformat(OBSERVED))

    def make(config, handler, *, enabled=True):
        path = tmp_path / "contract.json"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        settings = Settings(
            live_contract_path=path, live_submit_enabled=enabled, vendor_token="test-secret-v",
            erp_token="test-secret-e",
        )
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return HttpAdapter(settings, client=client)

    return make


def never_call(request):
    pytest.fail("此合同不能发出 HTTP 请求")


def test_open_api_password_login_is_lazy_and_reuses_token(contract, tmp_path):
    from app.adapters.http import HttpAdapter

    contract["services"]["erp"] = {
        "base_url": "http://103.198.125.2:16777", "allow_http_for_internal": True,
    }
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    settings = Settings(
        live_contract_path=contract_path, erp_token="", vendor_token="test-secret-v",
        erp_username="sample-user", erp_password="sample-password",
    )
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/open/system/innerOpen/login":
            assert json.loads(request.content) == {
                "username": "sample-user", "password": "sample-password",
            }
            return httpx.Response(200, json={
                "success": True, "code": 200, "data": {"accessToken": "sample-token"},
            })
        assert request.headers["Authorization"] == "Bearer sample-token"
        return httpx.Response(200, json={"success": True, "code": 200, "data": []})

    adapter = HttpAdapter(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert calls == []
    for _ in range(2):
        assert adapter._request("POST", "erp", "/open/shop/queryShop", json={})["success"]
    assert calls.count("/open/system/innerOpen/login") == 1


def test_open_api_credentials_never_sent_to_another_origin(contract, tmp_path):
    from app.adapters.http import HttpAdapter

    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    settings = Settings(
        live_contract_path=contract_path, erp_token="", vendor_token="test-secret-v",
        erp_username="sample-user", erp_password="sample-password",
    )
    adapter = HttpAdapter(settings, client=httpx.Client(transport=httpx.MockTransport(never_call)))
    with pytest.raises(SourceUnavailable):
        adapter._request("POST", "erp", "/open/shop/queryShop", json={})


def collection_response(request):
    data = {"timestamp": OBSERVED, "date": DAY.isoformat()}
    path = request.url.path
    if path.startswith("/candidates/"):
        assert json.loads(request.content) == {"day": "2026-11-05"}
        assert request.headers["authorization"] == "Bearer test-secret-e"
        data["rows"] = [ROW] if path.endswith("/loss_allocatable") else []
    elif path == "/online":
        assert dict(request.url.params) == {"day": "2026-11-05", "from": "2026-10-29", "to": "2026-11-04"}
        assert request.headers["authorization"] == "Bearer test-secret-v"
        data["rows"] = [ONLINE]
    else:
        assert path == "/fx"
        data.update(rate_day=DAY.isoformat(), rates={"MYR": "0.62", "CNY": "1"})
    return httpx.Response(200, json={"payload": data})


def test_collect_maps_four_sources_online_and_fx(contract, make_adapter):
    adapter = make_adapter(contract, collection_response)
    assert adapter.readiness()["contract_ready"] is True
    assert adapter.readiness()["verified_sites"] == ["MY"]
    snapshot = adapter.collect(DAY)
    assert snapshot.complete_sources == SOURCES
    assert snapshot.business_date == snapshot.fx_date == DAY
    assert snapshot.as_of.isoformat() == OBSERVED
    assert snapshot.fx_rates["MYR"] == Decimal("0.62")
    line = snapshot.lines[0]
    assert line.source == "loss_allocatable"
    assert line.key == VariantKey(**IDENTITY)
    assert line.loss_amount == Decimal("-0.9999999999999999")
    assert line.row_id == "0001" and "0001" in line.source_ref
    assert snapshot.online[0].fee_model.revenue_factor == Decimal("0.91")
    assert snapshot.online[0].fee_model.verified
    assert snapshot.source_versions["fx"] == "rates-v1"
    assert snapshot.source_versions["fee_model.MY"] == "fees-v1"


def test_missing_protection_mapping_blocks_collection_before_http(contract, make_adapter):
    del contract["collections"]["online"]["field_map"]["history_known"]
    adapter = make_adapter(contract, never_call)
    with pytest.raises(SourceUnavailable):
        adapter.collect(DAY)


def test_stale_source_cannot_be_relabelled_today(contract, make_adapter):
    def stale(request):
        response = collection_response(request)
        if request.url.path.endswith("/loss_allocatable"):
            data = response.json()
            data["payload"]["timestamp"] = "2026-11-04T09:00:00+08:00"
            return httpx.Response(200, json=data)
        return response

    with pytest.raises(SourceUnavailable, match="日期"):
        make_adapter(contract, stale).collect(DAY)


def test_pagination_cannot_silently_truncate(contract, make_adapter):
    contract["collections"]["loss_allocatable"]["pagination"] = {
        "page_key": "page", "limit_key": "limit", "limit": 1, "total_path": "payload.total", "max_pages": 2,
    }
    seen = []

    def paged(request):
        body = json.loads(request.content)
        seen.append(body["page"])
        assert body["limit"] == 1
        return httpx.Response(200, json={"payload": {
            "timestamp": OBSERVED, "date": DAY.isoformat(), "total": 3, "rows": [ROW],
        }})

    with pytest.raises(SourceUnavailable, match="分页"):
        make_adapter(contract, paged).collect(DAY)
    assert seen == [1, 2]


def test_http_error_sanitizes_request_and_response(contract, make_adapter):
    def http_error(request):
        return httpx.Response(403, json={"private": "https://access.example.test/?token=one-time"})

    with pytest.raises(SourceUnavailable) as captured:
        make_adapter(contract, http_error).collect(DAY)
    message = str(captured.value)
    assert not any(secret in message for secret in (".example.test", "test-secret", "one-time", "/candidates/"))
    assert captured.value.__suppress_context__


def decision():
    return Decision(
        key=VariantKey(**IDENTITY), status="待处理", ready=True, reason_code="ready", reason="",
        source_row_ids=["0001"], upload_column="直发仓税后价格", upload_price=Decimal("22.15"),
    )


def test_submit_creates_exact_template_and_real_multipart(contract, make_adapter):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/Api/api-tiktok-sales/operationLog/priceUpload"
        assert request.headers["authorization"] == "Bearer test-secret-v"
        assert request.headers["x-test-run"] == "exec-one"
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
        )
        part, = list(message.iter_parts())
        assert part.get_param("name", header="content-disposition") == "file"
        assert part.get_content_type() == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        book = load_workbook(BytesIO(part.get_payload(decode=True)))
        sheet = book["DATA-0"]
        assert [cell.value for cell in sheet[1]] == [
            "店铺名", "产品ID", "商品ID", "税前价格", "税后价格", "海外仓税前价格",
            "直发仓税前价格", "海外仓税后价格", "直发仓税后价格",
        ]
        assert [cell.value for cell in sheet[2]] == ["模板专用店名", "002", "003", None, None, None, None, None, 22.15]
        assert [cell.data_type for cell in sheet[2][:3]] == ["s", "s", "s"]
        return httpx.Response(200, json={"resp_code": 0, "resp_msg": "上传完成", "datas": {"job": "job-1"}})

    result = make_adapter(contract, handler).submit("exec-one", decision())
    assert result.status == "accepted" and result.task_id == "job-1"


@pytest.mark.parametrize("row,expected", [(2, "rejected"), (1, "unknown")])
def test_upload_zero_code_does_not_hide_row_errors(contract, make_adapter, row, expected):
    def handler(request):
        return httpx.Response(200, json={"resp_code": 0, "resp_msg": f"上传完成,第{row}行数据不存在在线列表"})

    assert make_adapter(contract, handler).submit("exec-one", decision()).status == expected


def test_submit_timeout_is_unknown_and_never_retried(contract, make_adapter):
    calls = []

    def timeout(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("https://secret.test/?token=secret", request=request)

    result = make_adapter(contract, timeout).submit("exec-one", decision())
    assert result.status == "unknown" and calls == ["POST"]
    assert "secret" not in result.message


def test_disabled_submit_does_not_call_http(contract, make_adapter):
    assert make_adapter(contract, never_call, enabled=False).submit("exec-one", decision()).status == "rejected"


@pytest.mark.parametrize(("change", "expected"), [
    ({}, "accepted"),
    ({"product": PRODUCT | {"warehouse": "overseas"}}, "unknown"),
    ({"readTime": None}, "unknown"),
    ({"readTime": "2026-11-05T09:00:00"}, "unknown"),
    ({"readTime": "2026-11-05T08:54:59+08:00", "effectiveTime": None}, "unknown"),
    ({"readTime": "2026-11-05T08:55:00+08:00", "effectiveTime": None}, "accepted"),
    ({"basis": "retail_including_vat"}, "unknown"),
    ({"ccy": "CNY"}, "unknown"),
    ({"ccy": None}, "unknown"),
    ({"value": None}, "unknown"),
    ({"state": "denied", "job": "different"}, "unknown"),
    ({"state": "denied", "value": None}, "rejected"),
])
def test_lookup_requires_actual_price_and_same_identity_or_bound_rejection(contract, make_adapter, change, expected):
    def handler(request):
        payload = json.loads(request.content)
        assert (payload["run"], payload["job"], payload["sku"], payload["shop"]) == ("exec-one", "job-1", "004", "001")
        return httpx.Response(200, json={
            "product": PRODUCT, "value": "11.075", "ccy": "MYR", "readTime": OBSERVED,
            "effectiveTime": OBSERVED, "basis": "actual_including_vat", "state": "applied", "job": "job-1",
        } | change)

    result = make_adapter(contract, handler).lookup("exec-one", "job-1", VariantKey(**IDENTITY))
    assert result.status == expected
    if expected == "accepted":
        assert result.actual_price == Decimal("11.075")
        assert result.observed_at.isoformat() == change.get("readTime", OBSERVED)


def test_missing_lookup_currency_mapping_disables_submission(contract, make_adapter):
    del contract["lookup"]["field_map"]["currency"]
    adapter = make_adapter(contract, never_call)
    assert not adapter.readiness()["contract_ready"]
    assert adapter.submit("exec-one", decision()).status == "rejected"


def test_missing_lookup_currency_response_remains_unknown(contract, make_adapter):
    def handler(request):
        return httpx.Response(200, json={
            "product": PRODUCT, "value": "11.075", "readTime": OBSERVED,
            "effectiveTime": OBSERVED, "basis": "actual_including_vat", "state": "applied", "job": "job-1",
        })

    result = make_adapter(contract, handler).lookup("exec-one", "job-1", VariantKey(**IDENTITY))
    assert result.status == "unknown" and result.actual_price is None


@pytest.mark.parametrize("warehouses", [None, [], ["overseas"]])
def test_upload_requires_confirmed_warehouse_scope(contract, make_adapter, warehouses):
    if warehouses is None:
        del contract["sites"]["MY"]["warehouses"]
    else:
        contract["sites"]["MY"]["warehouses"] = warehouses
    assert make_adapter(contract, never_call).submit("exec-one", decision()).status == "rejected"


@pytest.mark.parametrize("age", [None, 0, -1, True, "300"])
def test_lookup_needs_confirmed_positive_observation_age(contract, make_adapter, age):
    contract["lookup"]["max_age_seconds"] = age
    adapter = make_adapter(contract, never_call)
    assert not adapter.readiness()["contract_ready"]
    assert adapter.submit("exec-one", decision()).status == "rejected"


def test_example_is_disabled_before_any_request(make_adapter):
    path = Path(__file__).resolve().parents[1] / "deploy" / "live-contract.example.json"
    adapter = make_adapter(json.loads(path.read_text(encoding="utf-8")), never_call)
    assert not adapter.readiness()["contract_ready"]
    assert adapter.readiness()["missing"]
    with pytest.raises(SourceUnavailable):
        adapter.collect(DAY)
    assert adapter.submit("exec-one", decision()).status == "rejected"

def test_contract_cannot_define_token_query_parameters(contract, make_adapter):
    contract["collections"]["online"]["params"]["access_token"] = "embedded-credential"
    adapter = make_adapter(contract, never_call)
    assert not adapter.readiness()["contract_ready"]
    with pytest.raises(SourceUnavailable):
        adapter.collect(DAY)


def test_private_http_log_does_not_expose_service_urls(contract, make_adapter, caplog):
    with caplog.at_level("DEBUG"):
        make_adapter(contract, collection_response).collect(DAY)
    assert ".example.test" not in caplog.text


def test_numeric_task_echo_matches_upload_identifier(contract, make_adapter):
    def handler(request):
        if request.url.path.endswith("/priceUpload"):
            return httpx.Response(200, json={"resp_code": 0, "resp_msg": "上传完成", "datas": {"job": 42}})
        assert json.loads(request.content)["job"] == "42"
        return httpx.Response(200, json={
            "product": PRODUCT, "value": None, "ccy": None, "readTime": OBSERVED, "effectiveTime": None,
            "basis": "actual_including_vat", "state": "denied", "job": 42,
        })

    adapter = make_adapter(contract, handler)
    receipt = adapter.submit("exec-one", decision())
    assert receipt.task_id == "42"
    assert adapter.lookup("exec-one", receipt.task_id, VariantKey(**IDENTITY)).status == "rejected"


def test_redirect_is_never_followed_with_credentials(contract, make_adapter):
    calls = []

    def redirect(request):
        calls.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://other-origin.example.test/private"})

    with pytest.raises(SourceUnavailable):
        make_adapter(contract, redirect).collect(DAY)
    assert calls == ["erp.example.test"]
