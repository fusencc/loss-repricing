from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.erp_pricing import preview_pricing


ERP_HEADERS = ("订单编号", "平台", "店铺", "商品SKU列表", "订单数量", "SKU修正毛利", "客户邮箱")
SALES_HEADERS = ("店铺名称", "站点", "产品ID", "商品ID", "本地子sku", "平台子sku", "税前价")


def book(headers, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


@pytest.fixture
def sources():
    root = Path(__file__).parent / "fixtures"
    capture = json.loads((root / "erp-pricing-capture.sample.json").read_text(encoding="utf-8"))
    evidence = json.loads((root / "erp-pricing-evidence.sample.json").read_text(encoding="utf-8"))
    details, variants = [], {}
    for order in evidence["orders"]:
        for line in order["lines"]:
            mapping = line["mapping"]
            key = mapping["key"]
            local_sku = "ERP-" + key["sku"]
            qty = int(line["loss"]["quantity"]) if line["loss"] else 1
            details.append((order["order_id"], "Tiktok", mapping["erp_shop"], local_sku, qty, None, "ignored@example.test"))
            variants[key["item_id"]] = (mapping["erp_shop"], key["site"], key["product_id"], key["item_id"],
                                         local_sku, key["sku"], 19.95)
    return capture, evidence, details, list(variants.values())


def exports(details, variants):
    return book(ERP_HEADERS, details), book(SALES_HEADERS, variants)


def bind_refs(evidence, report):
    by_order = {}
    for row in report["export_mapping"]["rows"]:
        by_order.setdefault(row["order_id"], []).append(row["detail_ref"])
    for order in evidence["orders"]:
        for line, ref in zip(order["lines"], by_order[order["order_id"]], strict=True):
            line["detail_ref"] = ref


def test_export_mapping_keeps_source_rows_and_groups_online_targets(sources):
    capture, _, details, variants = sources
    report = preview_pricing(capture, exports=exports(details, variants))
    stage = report["export_mapping"]
    assert stage["summary"] == {
        "detail_rows": 4, "mapped_rows": 4, "unresolved_rows": 0,
        "unique_targets": 3, "cross_order_targets": 1, "sku_loss_present_rows": 0, "sku_loss_invalid_rows": 0,
    }
    first = stage["rows"][0]
    assert first["sources"] == ["loss_allocatable", "blocked_allocatable"]
    assert first["erp_sku"] == "ERP-SAMPLE-A"
    assert first["target"]["platform_sku"] == "SAMPLE-A"
    assert first["target"]["item_id"] == "0001001"
    assert "ignored@example.test" not in json.dumps(stage)
    assert report["decisions"] == []
    assert report["summary"]["normalized_detail_lines"] == 0
    issue = next(issue for issue in report["issues"] if issue["order_id"] == first["order_id"])
    assert issue["code"] == "export_detail_identity_pending"
    assert issue["export_detail_summary"] == {
        "detail_rows": 1, "mapped_rows": 1, "sku_loss_present_rows": 0, "sku_loss_invalid_rows": 0,
    }
    assert "SKU 修正毛利全空" in issue["reason"]


@pytest.mark.parametrize(("cell", "present", "invalid"), [
    ("  ", False, False), (0, True, False), ("未知", False, True), (True, False, True),
    (1e-7, True, False), (-1e-7, True, False), ("1e-7", False, True),
])
def test_export_sku_margin_counts_only_numeric_values(sources, cell, present, invalid):
    capture, _, details, variants = sources
    first = details[0]
    details[0] = (*first[:5], cell, first[6])
    report = preview_pricing(capture, exports=exports(details, variants))
    mapping = report["export_mapping"]
    assert mapping["rows"][0]["sku_loss_present"] is present
    assert mapping["rows"][0]["sku_loss_invalid"] is invalid
    assert mapping["summary"]["sku_loss_present_rows"] == int(present)
    assert mapping["summary"]["sku_loss_invalid_rows"] == int(invalid)
    assert not report["decisions"]



    capture, _, details, variants = sources
    first = variants[0]
    variants += [
        ("另一个店铺", *first[1:]),
        (*first[:3], "0001099", *first[4:]),
    ]
    report = preview_pricing(capture, exports=exports(details, variants))
    rows = report["export_mapping"]["rows"]
    assert [r["code"] for r in rows] == ["ambiguous_online_variant", "ambiguous_online_variant", "mapped", "mapped"]
    assert report["export_mapping"]["summary"]["mapped_rows"] == 2


def test_parent_ids_in_different_order_still_match(sources):
    capture, _, details, variants = sources
    order_id = details[0][0]
    rows = [row for group in capture["groups"] for row in group["rows"] if row["orderid"] == order_id]
    assert len(rows) == 2
    rows[0]["fields"]["item ID"] = "PARENT-A, EXTRA"
    rows[1]["fields"]["item ID"] = "EXTRA；PARENT-A"
    report = preview_pricing(capture, exports=exports(details, variants))
    assert report["export_mapping"]["rows"][0]["code"] == "mapped"


def test_finance_row_outside_captured_group_cannot_confirm_mapping(sources):
    capture, _, details, variants = sources
    capture["groups"][0]["rows"][0]["fields"]["集团"] = "另一集团"
    report = preview_pricing(capture, exports=exports(details, variants))
    assert report["export_mapping"]["rows"][0]["code"] == "finance_scope_mismatch"
    assert report["export_mapping"]["rows"][0]["target"] is None
    assert report["export_mapping"]["summary"]["mapped_rows"] == 3


def test_conflicting_captured_parent_sets_never_select_one_source(sources):
    capture, _, details, variants = sources
    overlap = next(group for group in capture["groups"] if group["source"] == "blocked_allocatable")
    overlap["rows"][0]["fields"]["item ID"] = "WRONG-PARENT"
    report = preview_pricing(capture, exports=exports(details, variants))
    assert report["export_mapping"]["rows"][0]["code"] == "conflicting_finance_identity"
    assert report["export_mapping"]["summary"]["mapped_rows"] == 3


def test_confirmed_evidence_flows_from_matching_exports_through_price_engine(sources):
    capture, evidence, details, variants = sources
    files = exports(details, variants)
    bind_refs(evidence, preview_pricing(capture, exports=files))
    report = preview_pricing(capture, evidence, exports=files)

    result = next(row for row in report["decisions"] if row["ready"])
    assert result["key"]["item_id"] == "0001001"
    assert Decimal(result["unit_loss"]) == Decimal("1.20")
    assert Decimal(result["upload_price"]) == Decimal("20.80")
    assert json.loads(result["selected_row_id"]) == ["000001", "01"]
    assert not report["execution_allowed"]
    assert report["export_mapping"]["evidence_conflicts"] == []


@pytest.mark.parametrize("corruption, code", [
    ("quantity", "evidence_quantity_mismatch"),
    ("sku", "evidence_target_mismatch"),
    ("reference", "evidence_export_coverage_mismatch"),
])
def test_raw_and_approved_evidence_cannot_silently_disagree(sources, corruption, code):
    capture, evidence, details, variants = sources
    files = exports(details, variants)
    bind_refs(evidence, preview_pricing(capture, exports=files))
    line = evidence["orders"][0]["lines"][0]
    if corruption == "quantity":
        line["loss"]["quantity"] = "99"
    elif corruption == "sku":
        line["mapping"]["key"]["sku"] = "UNRELATED"
    else:
        line["detail_ref"] = evidence["orders"][1]["lines"][0]["detail_ref"]

    report = preview_pricing(capture, evidence, exports=files)
    assert code in {issue["code"] for issue in report["export_mapping"]["evidence_conflicts"]}
    affected=line["mapping"]["key"]["item_id"]
    assert not any(row["ready"] for row in report["decisions"] if row["key"]["item_id"]==affected)


def test_two_physical_rows_of_the_same_order_and_sku_are_retained(sources):
    capture, evidence, details, variants = sources
    files = exports([*details, details[0]], variants)
    report = preview_pricing(capture, exports=files)
    rows = [row for row in report["export_mapping"]["rows"] if row["order_id"] == "000001"]
    assert len(rows) == 2 and len({row["detail_ref"] for row in rows}) == 2
    assert all(row["code"] == "mapped" for row in rows)
    assert report["decisions"] == []


def test_exports_can_be_uploaded_and_reopened_by_an_admin(sources, tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app.services.erp_capture_store import save_capture

    capture, evidence, details, variants = sources
    capture_id = save_capture(tmp_path, capture)["capture_id"]
    path = f"/api/sources/erp/captures/{capture_id}/pricing-previews"
    erp, sales = exports(details, variants)
    files = {"erp_details": ("erp.xlsx", erp), "sales_variants": ("sales.xlsx", sales)}
    cfg = Settings(_env_file=None, runtime_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'test.db'}",
                   admin_password="test-password", session_secret="s" * 40)
    with TestClient(create_app(cfg)) as client:
        assert client.post(path + "/from-exports", files=files).status_code == 401
        assert client.post("/api/auth/login", json={"username": "admin", "password": "test-password"}).is_success
        response = client.post(path + "/from-exports", files=files)
        assert response.status_code == 201, response.text
        report = response.json()
        assert report["export_mapping"]["summary"]["unique_targets"] == 3
        assert client.get(path + "/" + report["preview_id"]).json() == report

        evidence["capture_id"] = capture_id
        bind_refs(evidence, report)
        computed = client.post(
            path + "/from-exports", files=dict(files, evidence_json=("evidence.json", json.dumps(evidence))),
        )
        assert computed.status_code == 201, computed.text
        assert computed.json()["summary"]["ready_prices"] == 2
        assert client.get("/api/batches").json() == []
        malformed = client.post(path + "/from-exports", files=dict(files, sales_variants=("bad.xlsx", b"wrong")))
        assert malformed.status_code == 422
        too_large = dict(files, evidence_json=("large.json", b"x" * (5 * 1024 * 1024)))
        assert client.post(path + "/from-exports", files=too_large).status_code == 413
        assert len(list((tmp_path / "erp-pricing-previews").glob("*.json"))) == 2



def capture_fixture_workbooks(config, fixture, monkeypatch):
    from datetime import datetime

    import httpx

    from app.adapters import erp_bulk_capture as adapter

    groups = {(g["kind"], g["status"]): g for g in fixture["groups"]}
    capture_time = datetime.fromisoformat(fixture["queried_at"])

    class FixtureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return capture_time.astimezone(tz)

    monkeypatch.setattr(adapter, "datetime", FixtureClock)
    group = None

    def handle(request):
        nonlocal group
        path = request.url.path
        if path == "/fixture-finance.xlsx" or path == adapter.LOSS_EXPORT:
            if path == adapter.LOSS_EXPORT:
                group = groups["loss", json.loads(request.content)["status"]]
            fields = [row["fields"] for row in group["rows"]]
            headers = tuple(dict.fromkeys(["毛利", "修正毛利", *(key for row in fields for key in row)]))
            return httpx.Response(200, content=book(headers, [tuple(row.get(k) for k in headers) for row in fields]))
        body = json.loads(request.content)
        if path.endswith("/login"):
            return httpx.Response(200, json={"code": 200, "data": {"token": "fixture-token"}})
        if path == adapter.BLOCKED_LIST:
            group = groups["blocked", body["status"]]
            orders = [{"orderid": order, "status": group["status"], "jzfh": "1", "platform": "Tiktok"}
                      for order in group["candidate_order_ids"]]
            return httpx.Response(200, json={"code": 0, "data": {"total": len(orders), "records": orders}})
        if path == adapter.ORDER_EXPORT:
            assert body["orderIdList"] == group["candidate_order_ids"]
            return httpx.Response(200, json={"code": 200, "data": "下载任务【fixture】后台执行中"})
        assert path == adapter.TASKS
        return httpx.Response(200, json={"code": 200, "data": [{
            "source": "orderList", "fileName": "fixture", "id": group["source"],
            "status": "success", "downloadUrl": "/fixture-finance.xlsx",
        }]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = adapter.capture_erp_bulk(config, client=client)
    return dict(result, synthetic=True)


def test_selected_erp_preview_becomes_one_isolated_file_batch(sources, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from hashlib import sha256
    import sqlite3

    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.config import Settings
    from app.main import create_app
    from app.models import Batch, Execution, Job, Notice
    from app.models.coordination import TargetControl
    from app.services import store
    from app.services.erp_capture_store import save_capture
    from app.worker import Worker

    now = datetime(2026, 11, 11, 4, tzinfo=timezone.utc)
    monkeypatch.setattr(store, "utcnow", lambda: now)
    capture, evidence, details, variants = sources
    from app.api import erp_rates

    monkeypatch.setattr(erp_rates, "capture_erp_bulk",
                        lambda settings, **_: capture_fixture_workbooks(settings, capture, monkeypatch))
    erp, sales = exports(details, variants)
    files = {"erp_details": ("erp.xlsx", erp), "sales_variants": ("sales.xlsx", sales)}
    config = Settings(_env_file=None, runtime_dir=tmp_path, adapter_mode="file",
                      snapshot_path=tmp_path / "absent-default.json",
                      erp_username="fixture", erp_password="fixture-password", erp_capture_bloc="样例集团",
                      admin_password="test-password", session_secret="s" * 40)
    app = create_app(config)

    with TestClient(app) as client:
        capture_path = "/api/sources/erp/captures"
        assert client.post(capture_path).status_code == 401
        anonymous_reconciliation = f"{capture_path}/{capture['capture_id']}/pricing-previews/{'0' * 32}/reconciliation"
        assert client.post(anonymous_reconciliation).status_code == 401
        assert client.post("/api/auth/login", json={"username": "admin", "password": "test-password"}).is_success
        captured = client.post(capture_path)
        assert captured.is_success, captured.text
        metadata = captured.json()
        assert metadata["capture_complete"] and metadata["cleaning_status"] == "saved"
        capture_id = metadata["capture_id"]
        with sqlite3.connect(tmp_path / "erp-clean.sqlite3") as clean_db:
            assert clean_db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert clean_db.execute("SELECT count(*) FROM observations WHERE capture_id = ?", (capture_id,)).fetchone() == (5,)
        path = f"{capture_path}/{capture_id}/pricing-previews"
        raw = client.post(path + "/from-exports", files=files).json()
        assert client.post(f"{path}/{raw['preview_id']}/reconciliation").status_code == 409

        evidence["capture_id"] = capture_id
        bind_refs(evidence, raw)
        computed = client.post(path + "/from-exports", files=dict(
            files, evidence_json=("evidence.json", json.dumps(evidence))))
        assert computed.status_code == 201, computed.text
        report = computed.json()
        report_path = f"{path}/{report['preview_id']}"
        assert client.get(report_path).json() == report
        assert "_snapshot" not in report
        other_capture_id = save_capture(tmp_path, capture)["capture_id"]
        assert client.post(report_path.replace(capture_id, other_capture_id) + "/reconciliation").status_code == 404
        job_path = report_path + "/reconciliation"
        queued = client.post(job_path)
        assert queued.status_code == 202, queued.text
        assert client.post(job_path).json() == queued.json()
        job_id = queued.json()["job_id"]
        assert client.get("/api/batches").json() == []

        worker = Worker(app.state.factory, app.state.service)
        artifact = tmp_path / "erp-pricing-previews" / f"{report['preview_id']}.json"
        original = artifact.read_bytes()
        artifact.unlink()
        worker.tick(now.timestamp())
        assert client.get(f"/api/jobs/{job_id}").json()["state"] == "failed"
        assert client.get("/api/batches").json() == []
        # 误恢复同名报告也不能把已选中的建议价格替换为另一份内容。
        modified = json.loads(original)
        next(d for d in modified["decisions"] if d["ready"])["upload_price"] = "90.00"
        artifact.write_text(json.dumps(modified), encoding="utf-8")
        assert client.post(job_path).json() == queued.json()
        worker.tick(now.timestamp() + 1)
        assert client.get(f"/api/jobs/{job_id}").json()["state"] == "failed"
        assert client.get("/api/batches").json() == []

        artifact.write_bytes(original)
        assert client.post(job_path).json() == queued.json()
        worker.tick(now.timestamp() + 2)
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["state"] == "done", job
        batch_id = job["batch_id"]
        assert client.get(report_path).json()["reconciliation_batch_id"] == batch_id
        batch = client.get(f"/api/batches/{batch_id}").json()
        assert batch["mode"] == "file" and batch["status"] == "completed"
        assert batch["business_date"] == "2026-11-09"
        assert batch["erp_reconciliation"] == {
            "capture_id": capture_id, "preview_id": report["preview_id"],
            "synthetic": True, "evaluated_at": report["evaluated_at"],
            "artifact_sha256": sha256(original).hexdigest(),
        }
        assert sorted((i["decision"] for i in batch["items"]), key=lambda d: d["key"]["item_id"]) == sorted(
            report["decisions"], key=lambda d: d["key"]["item_id"]
        )
        ready = next(i for i in batch["items"] if i["decision"]["ready"])
        item_path = f"/api/items/{ready['id']}"
        assert ready["decision"]["upload_price"] == "20.80"
        assert ready["read_only"] and ready["phase"] == "blocked" and ready["execution"] is None
        assert client.get("/api/items").json() == []
        assert client.get("/api/overview").json()["latest_batch"] is None
        assert client.post(item_path + "/reevaluate").status_code == 409
        assert client.post(item_path + "/control", json={
            "version": 0, "action": "takeover", "reason": "test",
        }).status_code == 409

        artifact.write_text(json.dumps(modified), encoding="utf-8")
        assert client.get(report_path).status_code == 409
        assert client.post(job_path).status_code == 409
        artifact.write_bytes(original)

        # Worker恢复一个已持久化批次、但完成回执未写入的任务。
        with app.state.factory.begin() as session:
            stored = session.get(Batch, batch_id)
            assert stored.snapshot["source_versions"]["erp_capture_id"] == capture_id
            task = session.get(Job, job_id)
            task.state, task.lease_until, task.batch_id = "running", 0, None
        worker.tick(now.timestamp() + 3)
        assert client.get(f"/api/jobs/{job_id}").json()["batch_id"] == batch_id
        with app.state.factory() as session:
            assert session.scalar(select(func.count()).select_from(Batch)) == 1
            for model in (Execution, Notice, TargetControl):
                assert session.scalar(select(func.count()).select_from(model)) == 0

        sales_user = client.post("/api/users", json={
            "username": "sample-sales", "display_name": "样例销售", "password": "sales-password",
        }).json()
        assert client.post("/api/ownerships", json={
            **{name: ready["key"][name] for name in ("platform", "shop_id", "site")},
            "owner_id": sales_user["id"],
        }).is_success
        assert client.post("/api/auth/login", json={
            "username": "sample-sales", "password": "sales-password",
        }).is_success
        assert client.post(job_path).status_code == 403
        assert client.get("/api/batches").json() == []
        assert client.get(f"/api/batches/{batch_id}").status_code == 404
        assert client.get("/api/items", params={"batch_id": batch_id}).json() == []
        assert client.get(item_path).status_code == 404


def test_erp_preview_reconciliation_refuses_live_mode(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    config = Settings(_env_file=None, runtime_dir=tmp_path, adapter_mode="live",
                      admin_password="test-password", session_secret="s" * 40)
    with TestClient(create_app(config)) as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": "test-password"}).is_success
        capture = "erp-capture-20261109T063500000000Z-" + "0" * 32
        path = f"/api/sources/erp/captures/{capture}/pricing-previews/{'0' * 32}/reconciliation"
        assert client.post(path).status_code == 409

def test_observed_hash_in_platform_sku_is_preserved_through_evidence_and_calc(sources):
    capture, evidence, details, variants = sources
    platform_sku = "SAMPLE-A@PH#01"
    variants[0] = (*variants[0][:-2], platform_sku, variants[0][-1])
    for order in evidence["orders"]:
        for line in order["lines"]:
            key = line["mapping"]["key"]
            if key["sku"] == "SAMPLE-A":
                key["sku"] = platform_sku
    for row in evidence["online"]:
        if row["state"]["key"]["sku"] == "SAMPLE-A":
            row["state"]["key"]["sku"] = platform_sku
    files = exports(details, variants)
    mapping_report = preview_pricing(capture, exports=files)
    assert mapping_report["export_mapping"]["rows"][0]["target"]["platform_sku"] == platform_sku
    bind_refs(evidence, mapping_report)
    decisions = preview_pricing(capture, evidence, exports=files)["decisions"]
    decision = next(item for item in decisions if item["ready"])
    assert decision["key"]["sku"] == platform_sku
    assert Decimal(decision["upload_price"]) == Decimal("20.80")


def test_sparse_workbook_beyond_review_row_limit_is_rejected(sources):
    capture, _, details, variants = sources
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(SALES_HEADERS)
    sheet.append(variants[0])
    sheet["A1048576"] = variants[0][0]
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    with pytest.raises(ValueError, match="实际坐标超限"):
        preview_pricing(capture, exports=(book(ERP_HEADERS, details), buffer.getvalue()))


def test_formula_in_source_identity_is_not_used_as_literal_evidence(sources):
    capture, _, details, variants = sources
    row = details[0]
    details[0] = (*row[:3], '=CONCAT("ERP-", "SAMPLE-A")', *row[4:])
    with pytest.raises(ValueError, match="约定字段含公式"):
        preview_pricing(capture, exports=exports(details, variants))


def test_repeated_identical_sales_rows_do_not_amplify_report(sources):
    capture, _, details, variants = sources
    variants.extend([variants[0]] * 40)
    report = preview_pricing(capture, exports=exports(details, variants))
    assert report["export_mapping"]["summary"]["mapped_rows"] == 4
    assert all(len(row["sales_refs"]) == 1 for row in report["export_mapping"]["rows"])


def test_multi_sheet_workbook_cannot_claim_complete_export_coverage(sources):
    capture, _, details, variants = sources
    workbook = Workbook()
    workbook.active.append(SALES_HEADERS)
    workbook.active.append(variants[0])
    other = workbook.create_sheet("Extra")
    other.append(SALES_HEADERS)
    other.append((*variants[0][:3], "0001099", *variants[0][4:]))
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    with pytest.raises(ValueError, match="单工作表"):
        preview_pricing(capture, exports=(book(ERP_HEADERS, details), buffer.getvalue()))


def test_export_quantity_conflict_holds_only_the_identified_target(sources):
    capture, evidence, details, variants = sources
    files = exports(details, variants)
    bind_refs(evidence, preview_pricing(capture, exports=files))
    unrelated = evidence["orders"][1]["lines"][1]
    assert unrelated["mapping"]["key"]["item_id"] == "0001002"
    unrelated["loss"]["quantity"] = "99"

    report = preview_pricing(capture, evidence, exports=files)
    conflict, = report["export_mapping"]["evidence_conflicts"]
    assert conflict["detail_ref"] == unrelated["detail_ref"]
    assert conflict["code"] == "evidence_quantity_mismatch"
    assert Decimal(conflict["export_quantity"]) == Decimal("1")
    assert conflict["evidence_quantity"] == "99"
    assert [row["key"]["item_id"] for row in report["decisions"] if row["ready"]] == ["0001001"]
