from datetime import date, datetime, timedelta
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
from app.adapters.sales_auth import LOGIN_PATH, ORIGIN
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
    "seen": OBSERVED, "history": True,
    "last": None, "pending": False, "protected": False, "locked": False, "sellable": True,
    "profit": {"overseas_stock":"0","mode":"direct","department":"TEST",
        "inventory_observed_at":OBSERVED,"source_ref":"synthetic/profit",
        "model":{"revenue_factor":"0.91","fixed_cost":"9.8","fixed_cost_divisor":"1","version":"synthetic-online-v1","verified":True}},
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
        "observed_at": "seen", "protected": "protected",
        "activity_locked": "locked", "sellable": "sellable", "profit_check":"profit",
    }
    collection = {
        "version": "candidate-v1", "service": "erp", "method": "POST", "records_path": "payload.rows",
        "as_of_path": "payload.timestamp", "business_date_path": "payload.date", "field_map": row_fields,
        "constants": {"basis": "line", "fee_scope": "normal", "abnormal": False, "allocation_reliable": True},
    }
    return {
        "verified": True,
        "services": {
            "erp": {"base_url": "https://erp.example.test"},
            "vendor": {"base_url": ORIGIN, "allow_http_for_internal": True},
        },
        "collections": {
            **{source: collection | {"path": f"/candidates/{source}", "json": {"day": "{business_date}"}} for source in SOURCES},
            "online": collection | {
                "version": "online-v1", "service": "vendor", "method": "GET", "path": "/online",
                "params": {"day": "{business_date}"},
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
                   "upload_column": "直发仓税后价格", "version": "my-v1", "online_profit_model_version":"synthetic-online-v1",
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
            _env_file=None, live_contract_path=path, live_submit_enabled=enabled,
            vendor_api_url=ORIGIN, vendor_username="sample-sales", vendor_password="sample-password",
            erp_token="test-secret-e",
        )

        def handle_request(request):
            if request.url.path == LOGIN_PATH:
                assert request.url.host == httpx.URL(ORIGIN).host
                return httpx.Response(200, json={"access_token": "test-secret-v", "expires_in": 300})
            return handler(request)

        client = httpx.Client(transport=httpx.MockTransport(handle_request))
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
        _env_file=None, live_contract_path=contract_path, erp_token="",
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
        _env_file=None, live_contract_path=contract_path, erp_token="",
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
        assert dict(request.url.params) == {"day": "2026-11-05"}
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
    assert json.loads(line.row_id) == ["0002", "0001"] and line.row_id in line.source_ref
    assert snapshot.online[0].profit_check.model.verified
    assert snapshot.source_versions["fx"] == "rates-v1"


def test_live_http_does_not_require_or_trust_external_price_history(contract, make_adapter):
    for field in ('history_known','last_increase_at','pending_submission'):
        contract['collections']['online']['field_map'].pop(field,None)
    adapter=make_adapter(contract,collection_response)
    assert adapter.readiness()['contract_ready']
    state=adapter.collect(DAY).online[0]
    # 原始来源中的history/last/pending不参与；等待官方计数与本地未完成任务核对。
    assert state.history_known is False
    assert state.last_increase_at is None and state.pending_submission is False



def with_source_rows(**sources):
    def respond(request):
        response = collection_response(request)
        source = request.url.path.rsplit("/", 1)[-1]
        if source not in sources:
            return response
        payload = response.json()
        payload["payload"]["rows"] = sources[source]
        return httpx.Response(200, json=payload)
    return respond



def with_official_history(snapshot):
    """通过真实服务读取临时执行库，并注入明确的官方零计数。"""
    from types import SimpleNamespace
    from unittest.mock import patch
    from app.db import Base, create_database
    from app.services.execution import PricingService
    engine,factory=create_database('sqlite://')
    try:
        Base.metadata.create_all(engine)
        from datetime import timedelta
        def official(settings,keys,**kwargs):
            return {'source_id':'synthetic-official','window_start':(snapshot.as_of-timedelta(hours=72)).isoformat(),
                'window_end':snapshot.as_of.isoformat(),'records':{k.item_id:{'status':'ok','count':0} for k in keys}}
        with patch('app.services.execution.capture_recent_price_changes',official):
            return PricingService(factory,SimpleNamespace(mode='live'),Settings(_env_file=None))._enrich(snapshot)
    finally:
        engine.dispose()


def test_candidate_field_failure_holds_only_locatable_targets(contract, make_adapter):
    from app.domain.engine import evaluate

    good_key = {**PRODUCT, "item": "009", "sku": "OK"}
    bad_only_key = {**PRODUCT, "item": "010", "sku": "SOURCE@#"}
    handler = with_source_rows(
        loss_allocatable=[
            {**ROW, "loss": None},
            {**ROW, "order": "0008", "product": good_key, "loss": "-3.00"},
            {**ROW, "order": "0009", "product": bad_only_key, "qty": None},
        ],
        online=[ONLINE, {**ONLINE, "product": good_key}],
    )

    snapshot = make_adapter(contract, handler, enabled=False).collect(DAY)
    results = {decision.key.item_id: decision for decision in evaluate(with_official_history(snapshot), datetime.fromisoformat(OBSERVED))}

    assert snapshot.complete_sources == SOURCES
    assert results["003"].reason_code == "source_record_invalid"
    assert results["003"].unit_loss is None
    assert results["010"].reason_code == "source_record_invalid"
    assert results["010"].key.sku == "SOURCE@#"
    assert results["010"].trace["source_rows"] == []
    issue = results["010"].trace["source_issues"][0]
    assert issue["source"] == "loss_allocatable" and issue["record_number"] == 3
    assert json.loads(issue["row_id"]) == ["0009", ROW["detail"]]
    assert results["009"].ready and results["009"].upload_price == Decimal("22.00")


@pytest.mark.parametrize("missing", ["order", "product"])
def test_bad_candidate_does_not_stop_independent_target(contract, make_adapter, missing, caplog):
    from app.domain.engine import evaluate

    row = {**ROW, "loss": None}
    del row[missing]
    good_key = {**PRODUCT, "item": "009", "sku": "OK"}
    handler = with_source_rows(
        loss_allocatable=[row, {**ROW, "order": "0008", "product": good_key, "loss": "-3.00"}],
        online=[ONLINE, {**ONLINE, "product": good_key}],
    )
    snapshot = make_adapter(contract, handler).collect(DAY)
    results = {d.key.item_id: d for d in evaluate(with_official_history(snapshot), datetime.fromisoformat(OBSERVED))}

    assert results["009"].ready
    if missing == "order":
        assert results["003"].reason_code == "source_record_invalid"
        assert not snapshot.unlocated_candidate_rows
    else:
        assert "003" not in results and snapshot.unlocated_candidate_rows
        assert "第 1 条记录身份无法核实" in caplog.text


def test_presubmit_refresh_with_unlocated_row_still_processes_independent_target(contract, make_adapter, tmp_path, monkeypatch):
    from sqlalchemy import select

    from app.db import Base, create_database
    from app.models import Item
    from app.services.execution import PricingService
    from app.services.store import update_settings
    from test_user_pricing_policy import mock_official_zero

    good = {**PRODUCT, "item": "009", "sku": "OK"}
    losing = {**ROW, "product": good, "order": "0009", "loss": "-3.00"}
    first = with_source_rows(loss_allocatable=[ROW, losing], online=[ONLINE, {**ONLINE, "product": good}])
    refresh = with_source_rows(loss_allocatable=[{**ROW, "product": None}, losing],
                               online=[ONLINE, {**ONLINE, "product": good}])
    collections = 0

    def respond(request):
        nonlocal collections
        if request.url.path == "/candidates/loss_allocatable":
            collections += 1
        return (first if collections == 1 else refresh)(request)

    now = datetime.fromisoformat(OBSERVED)
    monkeypatch.setattr("app.services.execution.utcnow", lambda: now)
    mock_official_zero(monkeypatch, [now])
    adapter = make_adapter(contract, respond)
    settings = Settings(_env_file=None, runtime_dir=tmp_path, coordination_required=False, live_submit_enabled=True)
    engine, factory = create_database(f"sqlite:///{tmp_path / 'partial-refresh.db'}")
    try:
        Base.metadata.create_all(engine)
        update_settings(factory, {"autonomous_enabled": True, "pilot_shops": ["001"], "site_enabled": ["MY"]})
        service = PricingService(factory, adapter, settings)
        reached_submit = []
        monkeypatch.setattr(service, "_submit", lambda _, decision, day: reached_submit.append(decision.key.item_id))
        batch_id = service.run_batch("partial-refresh")
        assert collections == 2 and reached_submit == ["009"]
        with factory() as session:
            items = {item.key['item_id']: item for item in session.scalars(select(Item).where(Item.batch_id == batch_id))}
            assert items["003"].phase == "blocked" and items["003"].status == "待处理"
    finally:
        engine.dispose()


def test_invalid_candidate_still_counts_as_a_separate_order_line(contract, make_adapter):
    from app.domain.engine import evaluate

    # 同单第二行缺金额。第一行误标整单可分摊也不能拿整单金额算价。
    contract["collections"]["loss_allocatable"] = {
        **contract["collections"]["loss_allocatable"],
        "constants": {**contract["collections"]["loss_allocatable"]["constants"], "basis": "order"},
    }
    other_key = {**PRODUCT, "item": "008", "sku": "OTHER"}
    handler = with_source_rows(
        loss_allocatable=[
            {**ROW, "loss": "-3.00"},
            {**ROW, "product": other_key, "detail": "0005", "loss": None},
        ],
        loss_pending=[], blocked_allocatable=[], blocked_pending=[],
    )

    snapshot = make_adapter(contract, handler).collect(DAY)
    results = {decision.key.item_id: decision for decision in evaluate(with_official_history(snapshot), datetime.fromisoformat(OBSERVED))}

    assert results["003"].reason_code == "no_valid_loss"
    assert results["003"].trace["excluded_source_rows"] == {
        snapshot.lines[0].row_id: "unreliable_order_allocation",
    }
    assert results["008"].reason_code == "source_record_invalid"


@pytest.mark.parametrize("bad_main", ["0003", "CONFLICTING-MAIN"])
def test_bad_online_state_holds_only_its_target(contract, make_adapter, bad_main):
    from app.domain.engine import evaluate

    same_main_key = {**PRODUCT, "item": "008", "sku": "SIBLING"}
    unrelated_key = {**PRODUCT, "item": "009", "sku": "OTHER"}
    no_candidate_key = {**PRODUCT, "item": "010", "sku": "NO-ORDER"}
    handler = with_source_rows(
        loss_allocatable=[
            {**ROW, "loss": "-3.00"},
            {**ROW, "product": same_main_key, "order": "0006", "loss": "-3.00"},
            {**ROW, "product": unrelated_key, "order": "0007", "main": "UNRELATED", "loss": "-3.00"},
        ],
        loss_pending=[], blocked_allocatable=[], blocked_pending=[],
        online=[
            ONLINE,
            {**ONLINE, "main": bad_main, "r": None},
            {**ONLINE, "product": same_main_key},
            {**ONLINE, "product": unrelated_key, "main": "UNRELATED"},
            {**ONLINE, "product": no_candidate_key, "main": "NO-ORDER", "r": None},
        ],
    )

    snapshot = make_adapter(contract, handler).collect(DAY)
    results = {decision.key.item_id: decision for decision in evaluate(with_official_history(snapshot), datetime.fromisoformat(OBSERVED))}

    assert {state.key.item_id for state in snapshot.online} == {"008", "009"}
    assert results["003"].reason_code == "source_record_invalid"
    assert results["008"].ready and "source_issues" not in results["008"].trace
    assert results["003"].trace["source_issues"][0]["record_number"] == 2
    assert results["009"].ready
    assert "010" not in results



def test_unrelated_field_issues_remain_in_snapshot_not_sibling_decisions(contract, make_adapter):
    from app.domain.engine import evaluate

    candidates = [
        {**ROW, "product": {**PRODUCT, "item": f"candidate-{n}", "sku": f"SKU-{n}"},
         "order": f"ORDER-{n}", "loss": "-3.00"}
        for n in range(10)
    ]
    online = [{**ONLINE, "product": row["product"]} for row in candidates]
    online.extend(
        {**ONLINE, "product": {**PRODUCT, "item": f"bad-{n}", "sku": f"BAD-{n}"}, "r": None}
        for n in range(25)
    )
    handler = with_source_rows(loss_allocatable=candidates, loss_pending=[], blocked_allocatable=[],
                               blocked_pending=[], online=online)

    snapshot = make_adapter(contract, handler, enabled=False).collect(DAY)
    results = evaluate(with_official_history(snapshot), datetime.fromisoformat(OBSERVED))

    assert len(snapshot.source_issues) == 25 and len(results) == 10
    for result in results:
        assert result.ready and 'source_issues' not in result.trace



def test_worker_persists_bad_source_and_prices_independent_target(contract, make_adapter, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.main import create_app
    from app.models import Execution
    from app.services import execution, store
    from app.worker import Worker

    good_key = {**PRODUCT, "item": "009", "sku": "OK"}
    respond = with_source_rows(
        loss_allocatable=[{**ROW, "loss": None}, {**ROW, "product": good_key, "order": "0008", "loss": "-3.00"}],
        online=[ONLINE, {**ONLINE, "product": good_key}],
    )
    requests = []

    def record(request):
        requests.append(request.url.path)
        return respond(request)

    adapter = make_adapter(contract, record, enabled=False)
    now = datetime.fromisoformat(OBSERVED)
    monkeypatch.setattr(execution, "utcnow", lambda: now)
    from test_user_pricing_policy import mock_official_zero
    mock_official_zero(monkeypatch,[now])
    monkeypatch.setattr(store, "utcnow", lambda: now)
    monkeypatch.setattr("app.main.build_adapter", lambda config: adapter)
    settings = Settings(
        _env_file=None, adapter_mode="live", runtime_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'source-issue-test.db'}",
        admin_password="test-local-password", session_secret="s" * 40,
        live_submit_enabled=False, coordination_required=False,
    )

    app = create_app(settings)
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={
            "username": "admin", "password": "test-local-password",
        }).is_success
        queued = client.post("/api/batches/run")
        assert queued.is_success, queued.text
        Worker(app.state.factory, app.state.service).tick(now.timestamp())
        job = client.get("/api/jobs/" + queued.json()["job_id"]).json()
        assert job["state"] == "done", job
        batch_path = "/api/batches/" + job["batch_id"]
        batch = client.get(batch_path).json()
        assert batch["source_issue_count"] == 1
        results = {item["key"]["item_id"]: item for item in batch["items"]}
        assert results["003"]["decision"]["reason_code"] == "source_record_invalid"
        assert results["009"]["decision"]["ready"] and results["009"]["decision"]["upload_price"] == "22.00"
        source_issue = client.get(batch_path + "/source-issues").json()["items"][0]
        assert source_issue["source"] == "loss_allocatable" and source_issue["record_number"] == 1
        assert source_issue["key"]["item_id"] == "003"
        with app.state.factory() as session:
            assert session.scalar(select(func.count()).select_from(Execution)) == 0

    assert requests == [*(f"/candidates/{source}" for source in SOURCES), "/online", "/fx"]



@pytest.mark.parametrize("shape", ["same_scope_count", "target_membership", "stale_count"])
def test_well_formed_sales_conflict_cannot_price_affected_main_skus(contract, make_adapter, shape):
    from app.domain.engine import evaluate

    sibling = {**PRODUCT, "item": "008", "sku": "SIBLING"}
    independent = {**PRODUCT, "item": "009", "sku": "OTHER"}
    main = "ALTERNATE" if shape == "target_membership" else ONLINE["main"]
    sibling_state = {**ONLINE, "product": sibling, "main": main, "qty": 21}
    online = [ONLINE, sibling_state, {**ONLINE, "product": independent, "main": "UNRELATED"}]
    if shape == "target_membership":
        sibling_state["qty"] = 10  # 单靠销量差异无法复现归属冲突。
        online.append({**ONLINE, "main": "ALTERNATE"})
    elif shape == "stale_count":
        sibling_state["seen"] = "2026-11-04T09:00:00+08:00"

    respond = with_source_rows(
        loss_allocatable=[
            {**ROW, "loss": "-3.00"},
            {**ROW, "product": sibling, "main": main, "order": "0006", "loss": "-3.00"},
            {**ROW, "product": independent, "main": "UNRELATED", "order": "0007", "loss": "-3.00"},
        ],
        loss_pending=[], blocked_allocatable=[], blocked_pending=[], online=online,
    )
    snapshot = make_adapter(contract, respond).collect(DAY)
    results = {result.key.item_id: result for result in evaluate(with_official_history(snapshot), datetime.fromisoformat(OBSERVED))}

    assert not snapshot.source_issues  # 字段均合规；问题来自有效记录之间的证据冲突。
    assert results["009"].ready
    if shape == "stale_count":
        assert results["003"].ready and not results["008"].ready
    elif shape == "same_scope_count":
        assert results["003"].ready and results["008"].ready
    else:
        assert results["003"].reason_code == results["008"].reason_code == "conflicting_main_sku_sales"




def test_missing_online_warehouse_only_holds_its_target(contract, make_adapter):
    contract["collections"]["online"] = {
        **contract["collections"]["online"],
        "pagination": {
            "page_key": "page", "limit_key": "limit", "limit": 1, "total_path": "payload.total", "max_pages": 1,
        },
    }

    def respond(request):
        if request.url.path != "/online":
            return collection_response(request)
        product = {field: value for field, value in PRODUCT.items() if field != "warehouse"}
        return httpx.Response(200, json={"payload": {
            "timestamp": OBSERVED, "date": DAY.isoformat(), "total": 1,
            "rows": [{**ONLINE, "product": product}],
        }})

    snapshot = make_adapter(contract, respond).collect(DAY)
    from app.domain.engine import evaluate

    assert snapshot.online == []
    assert snapshot.source_issues[0].key.warehouse is None
    [result] = evaluate(snapshot, datetime.fromisoformat(OBSERVED))
    assert result.reason_code == "source_record_invalid"


def test_missing_candidate_warehouse_retains_scope_without_guessing(contract, make_adapter):
    from app.domain.engine import evaluate
    from app.domain.types import Snapshot

    incomplete_key = {field: value for field, value in PRODUCT.items() if field != "warehouse"}
    good_key = {**PRODUCT, "item": "009", "sku": "INDEPENDENT"}
    no_online_key = {**incomplete_key, "item": "010", "sku": "NO-ONLINE"}
    respond = with_source_rows(
        loss_allocatable=[
            {**ROW, "product": incomplete_key, "loss": "-3.00"},
            {**ROW, "product": good_key, "order": "0008", "loss": "-3.00"},
            {**ROW, "product": no_online_key, "order": "0009", "loss": "-3.00"},
        ],
        loss_pending=[], blocked_allocatable=[], blocked_pending=[],
        online=[ONLINE, {**ONLINE, "product": good_key}],
    )
    snapshot = make_adapter(contract, respond).collect(DAY)
    snapshot = Snapshot.model_validate_json(snapshot.model_dump_json())
    results = {decision.key.item_id: decision for decision in evaluate(with_official_history(snapshot), datetime.fromisoformat(OBSERVED))}

    assert snapshot.complete_sources == SOURCES
    assert len(snapshot.source_issues) == 2
    assert all(issue.key.warehouse is None for issue in snapshot.source_issues)
    assert results["003"].reason_code == "source_record_invalid"
    assert results["003"].trace["source_issues"][0]["key"]["warehouse"] is None
    assert results["003"].key.warehouse == PRODUCT["warehouse"]  # 单独已确认的在线身份仅用于定位。
    assert results["009"].ready
    assert "010" not in results  # 没有完整在线身份，仅在批次来源异常中显示。


def test_missing_protection_mapping_blocks_collection_before_http(contract, make_adapter):
    del contract["collections"]["online"]["field_map"]["protected"]
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


@pytest.mark.parametrize("second_order", ["0009", ROW["order"]])
def test_paginated_detail_ids_are_scoped_to_orders(contract, make_adapter, second_order):
    from app.domain.engine import evaluate

    contract["collections"]["loss_allocatable"]["pagination"] = {
        "page_key": "page", "limit_key": "limit", "limit": 1, "total_path": "payload.total", "max_pages": 2,
    }

    def handler(request):
        if not request.url.path.endswith("/loss_allocatable"):
            return collection_response(request)
        page = json.loads(request.content)["page"]
        row = ROW if page == 1 or second_order == ROW["order"] else {**ROW, "order": second_order, "loss": "-0.6"}
        return httpx.Response(200, json={"payload": {
            "timestamp": OBSERVED, "date": DAY.isoformat(), "total": 2, "rows": [row],
        }})

    snapshot = make_adapter(contract, handler).collect(DAY)
    result = evaluate(with_official_history(snapshot), datetime.fromisoformat(OBSERVED))[0]
    assert result.ready
    if second_order == ROW["order"]:
        assert len(snapshot.lines) == 1
    else:
        assert len(snapshot.lines) == 2
        assert result.unit_loss == Decimal("0.2")
        assert json.loads(result.selected_row_id) == [second_order, ROW["detail"]]


def test_paginated_source_cannot_count_one_detail_twice_under_different_products(contract, make_adapter):
    contract["collections"]["loss_allocatable"]["pagination"] = {
        "page_key": "page", "limit_key": "limit", "limit": 1, "total_path": "payload.total", "max_pages": 2,
    }

    def handler(request):
        if not request.url.path.endswith("/loss_allocatable"):
            return collection_response(request)
        page = json.loads(request.content)["page"]
        row = ROW if page == 1 else {**ROW, "product": {**PRODUCT, "item": "009", "sku": "009"}}
        return httpx.Response(200, json={"payload": {
            "timestamp": OBSERVED, "date": DAY.isoformat(), "total": 2, "rows": [row],
        }})

    from app.domain.engine import evaluate

    snapshot = make_adapter(contract, handler).collect(DAY)
    results = evaluate(snapshot, datetime.fromisoformat(OBSERVED))
    assert len(results) == 2
    assert {result.reason_code for result in results} == {"conflicting_detail_mapping"}


@pytest.mark.parametrize("price_changed", [False, True])
def test_paginated_online_repeats_only_hold_the_conflicting_target(contract, make_adapter, price_changed):
    from app.domain.engine import evaluate

    contract["collections"]["online"]["pagination"] = {
        "page_key": "page", "limit_key": "limit", "limit": 2, "total_path": "payload.total", "max_pages": 2,
    }
    good = {**PRODUCT, "item": "009", "sku": "OK"}
    candidates = with_source_rows(loss_allocatable=[ROW, {**ROW, "order": "0009", "product": good}])

    def respond(request):
        if request.url.path != "/online":
            return candidates(request)
        page = request.url.params["page"]
        rows = ([ONLINE, {**ONLINE, "product": good}] if page == "1" else
                [{**ONLINE, "r": "23"} if price_changed else ONLINE])
        return httpx.Response(200, json={"payload": {
            "timestamp": OBSERVED, "date": DAY.isoformat(), "total": 3, "rows": rows,
        }})

    snapshot = make_adapter(contract, respond).collect(DAY)
    results = {d.key.item_id: d for d in evaluate(with_official_history(snapshot), datetime.fromisoformat(OBSERVED))}
    assert results["009"].ready
    if price_changed:
        assert {state.key.item_id for state in snapshot.online} == {"009"}
        assert results["003"].reason_code == "source_record_invalid"
    else:
        assert len(snapshot.online) == 2 and not snapshot.source_issues
        assert results["003"].ready


def test_pagination_keeps_valid_rows_when_total_and_observation_time_change(contract, make_adapter):
    contract['collections']['loss_allocatable']['pagination'] = {
        'page_key': 'page', 'limit': 1, 'total_path': 'payload.total', 'max_pages': 2,
    }

    def handler(request):
        if not request.url.path.endswith('/loss_allocatable'):
            return collection_response(request)
        page = json.loads(request.content)['page']
        row = ROW if page == 1 else {**ROW, 'order': '0009'}
        timestamp = datetime.fromisoformat(OBSERVED) + timedelta(seconds=page - 1)
        return httpx.Response(200, json={'payload': {
            'timestamp': timestamp.isoformat(), 'date': DAY.isoformat(), 'total': 4 - page, 'rows': [row],
        }})

    snapshot = make_adapter(contract, handler).collect(DAY)
    assert {row.order_id for row in snapshot.lines} == {ROW['order'], '0009'}
    assert snapshot.as_of == datetime.fromisoformat(OBSERVED)


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
            "店铺名（必填）", "产品ID（必填）", "商品ID（必填）", "税前价格", "税后价格", "海外仓税前价格",
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
    ({"job": "different"}, "unknown"),
    ({"run": "different"}, "unknown"),
    ({"state": "denied", "run": "different"}, "unknown"),
    ({"state": "denied", "job": "different"}, "unknown"),
    ({"state": "denied", "value": None}, "rejected"),
])
def test_lookup_requires_actual_price_and_same_identity_or_bound_rejection(contract, make_adapter, change, expected):
    contract["lookup"]["field_map"]["execution_id"] = "run"
    def handler(request):
        payload = json.loads(request.content)
        assert (payload["run"], payload["job"], payload["sku"], payload["shop"]) == ("exec-one", "job-1", "004", "001")
        return httpx.Response(200, json={
            "product": PRODUCT, "value": "11.075", "ccy": "MYR", "readTime": OBSERVED,
            "effectiveTime": OBSERVED, "basis": "actual_including_vat", "state": "applied", "job": "job-1", "run": "exec-one",
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


@pytest.mark.parametrize("change", ["unapproved_version", "string_verified", "float_cost"])
def test_online_profit_http_contract_rejects_unapproved_or_coerced_proof(contract, change):
    import copy
    from app.adapters.normalization import online_state
    raw=copy.deepcopy(ONLINE)
    if change=="unapproved_version":raw["profit"]["model"]["version"]="personal-old-v1"
    elif change=="string_verified":raw["profit"]["model"]["verified"]="true"
    else:raw["profit"]["model"]["fixed_cost"]=9.8
    if change=="unapproved_version":
        state=online_state(raw,contract["collections"]["online"],{"MY":"synthetic-online-v1"})
        assert state.profit_check.model.verified is False
    else:
        with pytest.raises(ValueError):
            online_state(raw,contract["collections"]["online"],{"MY":"synthetic-online-v1"})


