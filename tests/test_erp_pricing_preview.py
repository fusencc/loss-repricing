import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.erp_pricing import preview_pricing
from app.domain.engine import FORMULA_VERSION


@pytest.fixture
def source_files():
    fixture_dir = Path(__file__).parent / "fixtures"
    capture = json.loads((fixture_dir / "erp-pricing-capture.sample.json").read_text(encoding="utf-8"))
    evidence = json.loads((fixture_dir / "erp-pricing-evidence.sample.json").read_text(encoding="utf-8"))
    return capture, evidence


def test_saved_capture_without_supplement_identifies_orders_missing_details(source_files):
    capture, _ = source_files
    report = preview_pricing(capture)
    assert report["capture_id"] == capture["capture_id"]
    assert report["summary"]["source_rows"] == 5
    assert report["summary"]["unique_orders"] == 3
    assert report["decisions"] == []
    assert report["evaluation_complete_sources"] == []
    assert [
        issue["sources"] for issue in report["issues"]
        if issue["order_id"] == "000001" and issue["code"] == "missing_order_details"
    ] == [["loss_allocatable", "blocked_allocatable"]]


def test_replay_deduplicates_by_detail_and_uses_the_existing_price_formula(source_files):
    capture, evidence = source_files
    # 同单同 SKU 的另一条真实行必须保留。与主明细 ID 相同的跨来源行只算一次。
    order = evidence["orders"][1]
    second_same_sku = dict(order["lines"][0], detail_id="03", detail_ref="sample-erp-detail/000002/03")
    second_same_sku["loss"] = dict(second_same_sku["loss"], amount="-1.80", ref="sample-loss/000002/03")
    order["lines"].append(second_same_sku)

    report = preview_pricing(capture, evidence)

    assert report["synthetic"] is True
    assert report["execution_allowed"] is False
    decisions = {item["key"]["item_id"]: item for item in report["decisions"]}
    decision = decisions["0001001"]
    assert decision["ready"]
    assert Decimal(decision["unit_loss"]) == Decimal("1.20")
    assert Decimal(decision["current_actual"]) == Decimal("18")
    assert Decimal(decision["target_actual"]) == Decimal("18.72")
    assert Decimal(decision["upload_price"]) == Decimal("20.80")
    assert decision["trace"]["formula_version"] == FORMULA_VERSION
    assert json.loads(decision["selected_row_id"]) == ["000001", "01"]
    assert {tuple(json.loads(row_id)) for row_id in decision["source_row_ids"]} == {
        ("000001", "01"), ("000002", "01"), ("000002", "03")
    }
    first_line = [row for row in decision["trace"]["source_rows"] if row["order_id"] == "000001"]
    assert [row["source"] for row in first_line] == ["loss_allocatable", "blocked_allocatable"]
    assert decisions["0001002"]["reason_code"] == "ready"
    issue = next(issue for issue in report["issues"] if issue["code"] == "missing_line_loss")
    assert issue["order_id"] == "000003"
    assert issue["target"]["item_id"] == "0001003"


def test_a_missing_loss_holds_the_same_target_across_orders(source_files):
    capture, evidence = source_files
    evidence["orders"][2]["lines"][0]["mapping"] = evidence["orders"][0]["lines"][0]["mapping"]
    report = preview_pricing(capture, evidence)
    assert [decision["key"]["item_id"] for decision in report["decisions"]] == ["0001002"]
    assert any(issue["target"]["item_id"] == "0001001" for issue in report["issues"])


def test_unmapped_line_makes_minimum_loss_unprovable_for_the_scope(source_files):
    capture, evidence = source_files
    evidence["orders"][2]["lines"][0]["mapping"] = None
    report = preview_pricing(capture, evidence)
    assert "loss_pending" not in report["evaluation_complete_sources"]
    assert report["decisions"]
    assert all(decision["reason_code"] == "incomplete_sources" for decision in report["decisions"])
    assert any(issue["code"] == "missing_online_mapping" for issue in report["issues"])


def test_status_change_holds_only_known_targets_of_the_order(source_files):
    capture, evidence = source_files
    capture["groups"][0]["rows"][0]["current_status_matches_source"] = False
    capture["groups"][0]["rows"][0]["fields"]["订单状态"] = "作废"
    report = preview_pricing(capture, evidence)
    assert [decision["key"]["item_id"] for decision in report["decisions"]] == ["0001002"]
    assert any(issue["order_id"] == "000001" and issue["code"] == "order_status_conflict"
               for issue in report["issues"])


@pytest.mark.parametrize("change", ["missing_source", "finance_duplicate", "sample", "mixed_group"])
def test_incomplete_or_mixed_capture_cannot_produce_ready_prices(source_files, change):
    capture, evidence = source_files
    if change == "missing_source":
        capture["groups"].pop()
    elif change == "finance_duplicate":
        capture["groups"][0]["rows"].append(capture["groups"][0]["rows"][0])
    elif change == "sample":
        capture["sample_only"] = True
    else:
        capture["groups"][3]["filters"]["blocs"] = "另一集团"
    report = preview_pricing(capture, evidence)
    assert len(report["evaluation_complete_sources"]) < 4
    assert not any(item["ready"] for item in report["decisions"])


@pytest.mark.parametrize("defect", ["failed", "overlap", "unrequested", "last_poll_failed", "swapped_requests", "wrong_count", "repeated_number"])
def test_group_summary_cannot_hide_incomplete_finance_batches(source_files, defect):
    capture, evidence = source_files
    pending = capture["groups"][1]
    first, second = [
        dict(pending["batches"][0], number=index, order_ids=[order], scope_check={
            "requested": 1, "downloaded_rows": 1, "downloaded_unique": 1,
            "missing": [], "unexpected": [], "duplicate_rows": 0,
        })
        for index, order in enumerate(pending["candidate_order_ids"], 1)
    ]
    if defect == "failed":
        second.update(status="failed", polls=[])
    elif defect == "overlap":
        second["order_ids"] = first["order_ids"]
    elif defect == "unrequested":
        second["order_ids"] = ["outside"]
    elif defect == "last_poll_failed":
        second["polls"] = [*second["polls"], dict(second["polls"][-1], status="failure")]
    elif defect == "swapped_requests":
        first["order_ids"], second["order_ids"] = second["order_ids"], first["order_ids"]
    elif defect == "wrong_count":
        second["scope_check"]["downloaded_rows"] = 2
    else:
        second["number"] = first["number"]
    pending["batches"] = [first, second]

    report = preview_pricing(capture, evidence)
    source = next(row for row in report["source_coverage"] if row["source"] == pending["source"])
    assert not source["finance_complete"] and not source["preparation_complete"]
    assert report["summary"]["ready_prices"] == 0
    assert any(message.startswith(pending["source"] + " 标记完成") for message in report["global_issues"])


@pytest.mark.parametrize("change", ["capture_id", "synthetic", "evaluated_at"])
def test_evidence_is_bound_to_the_same_capture_and_business_day(source_files, change):
    capture, evidence = source_files
    evidence[change] = {"capture_id": "other", "synthetic": False,
                        "evaluated_at": "2026-11-10T15:00:00+08:00"}[change]
    with pytest.raises(ValueError):
        preview_pricing(capture, evidence)


def test_admin_can_replay_and_reopen_a_saved_capture_without_creating_execution(source_files, tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app
    from app.services.erp_capture_store import save_capture

    capture, evidence = source_files
    stored = save_capture(tmp_path, capture)
    capture_id = stored["capture_id"]
    evidence["capture_id"] = capture_id
    path = f"/api/sources/erp/captures/{capture_id}/pricing-previews"
    settings = Settings(_env_file=None, runtime_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'preview.db'}",
                        admin_password="test-password", session_secret="s" * 40)
    with TestClient(create_app(settings)) as client:
        assert client.post(path, json=evidence).status_code == 401
        login = client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
        assert login.status_code == 200

        response = client.post(path, json=evidence)
        assert response.status_code == 201, response.text
        report = response.json()
        assert report["capture_id"] == capture_id and report["summary"]["ready_prices"] == 2
        report_path = f"{path}/{report['preview_id']}"
        assert client.get(report_path).json() == report
        assert client.get(f"{path}/bad-id").status_code == 422
        assert client.get(f"{path}/{'0' * 32}").status_code == 404
        other_capture = save_capture(tmp_path, capture)["capture_id"]
        assert client.get(report_path.replace(capture_id, other_capture)).status_code == 404

        assert client.get("/api/batches").json() == []
        assert list((tmp_path / "demo-receipts").iterdir()) == []
        client.post("/api/auth/logout")
        assert client.get(report_path).status_code == 401


def test_incomplete_snapshot_dates_and_online_states_fail_without_inventing_values(source_files):
    capture, evidence = source_files
    evidence["fx"]["date"] = "2026-11-08"
    report = preview_pricing(capture, evidence)
    assert all(decision["reason_code"] == "invalid_fx" for decision in report["decisions"])

    evidence["fx"]["date"] = "2026-11-09"
    evidence["online"].pop(0)
    report = preview_pricing(capture, evidence)
    decisions = {item["key"]["item_id"]: item for item in report["decisions"]}
    assert decisions["0001001"]["reason_code"] == "missing_mapping"
    assert decisions["0001002"]["reason_code"] == "ready"


def test_online_price_cannot_be_observed_before_its_change(source_files):
    capture, evidence = source_files
    evidence["online"][0]["state"]["last_changed_at"] = "2026-11-09T14:55:00+08:00"
    report = preview_pricing(capture, evidence)
    decisions = {row["key"]["item_id"]: row for row in report["decisions"]}
    assert decisions["0001001"]["reason_code"] == "stale_online"
    assert not decisions["0001001"]["ready"]
    assert decisions["0001002"]["ready"]


def test_different_shop_on_the_same_order_is_reported_before_pricing(source_files):
    capture, evidence = source_files
    capture["groups"][0]["rows"][0]["fields"]["店铺"] = "另一店铺"
    report = preview_pricing(capture, evidence)
    assert {decision["key"]["item_id"] for decision in report["decisions"]} == {"0001001", "0001002"}
    assert any(issue["code"] == "order_shop_conflict" and issue["order_id"] == "000001"
               for issue in report["issues"])
    assert "loss_allocatable" not in report["evaluation_complete_sources"]
    assert "blocked_allocatable" not in report["evaluation_complete_sources"]
    assert all(decision["reason_code"] == "incomplete_sources" for decision in report["decisions"])


def test_finance_without_shop_cannot_confirm_an_online_mapping(source_files):
    capture, evidence = source_files
    for group in capture["groups"]:
        for row in group["rows"]:
            if row["orderid"] == "000001":
                row["fields"]["店铺"] = " "
    report = preview_pricing(capture, evidence)
    assert any(issue["code"] == "missing_order_shop" and issue["order_id"] == "000001"
               for issue in report["issues"])
    assert not any(decision["ready"] for decision in report["decisions"])


def test_unconfirmed_extra_capture_filter_prevents_coverage(source_files):
    capture, evidence = source_files
    capture["groups"][0]["filters"]["starttime"] = "2026-11-01"
    report = preview_pricing(capture, evidence)
    assert report["evaluation_complete_sources"] == []
    assert not any(message.startswith("loss_allocatable 标记完成") for message in report["global_issues"])
    assert not any(decision["ready"] for decision in report["decisions"])


def test_known_missing_finance_holds_its_mapped_target_and_independent_pricing_continues(source_files):
    capture, evidence = source_files
    capture["capture_complete"] = False
    pending = capture["groups"][1]
    pending["rows"].pop()
    pending.update(stage="complete_with_missing", error="部分财务未返回")
    pending["batches"][0]["status"] = "partial"
    pending["batches"][0]["scope_check"].update(downloaded_rows=1, downloaded_unique=1, missing=["000003"])
    report = preview_pricing(capture, evidence)
    coverage = next(row for row in report["source_coverage"] if row["source"] == "loss_pending")
    assert not coverage["finance_complete"] and coverage["preparation_complete"]
    assert any(issue["order_id"] == "000003" and issue["code"] == "missing_order_finance"
               for issue in report["issues"])
    assert any(decision["key"]["item_id"] == "0001001" and decision["ready"]
               for decision in report["decisions"])


@pytest.mark.parametrize("target_known", [True, False])
def test_duplicate_finance_rows_hold_identified_target_without_masking_source_conflict(source_files, target_known):
    capture, evidence = source_files
    capture["capture_complete"] = False
    pending = capture["groups"][1]
    pending["rows"].append(pending["rows"][-1])
    pending.update(stage="finance_conflict", error="存在重复财务行")
    pending["batches"][0]["status"] = "partial"
    pending["batches"][0]["scope_check"].update(downloaded_rows=3, duplicate_rows=1)
    if not target_known:
        evidence["orders"][2]["lines"][0]["mapping"] = None

    report = preview_pricing(capture, evidence)
    coverage = next(row for row in report["source_coverage"] if row["source"] == "loss_pending")
    assert not coverage["finance_complete"]
    assert coverage["preparation_complete"] is target_known
    assert any(issue["order_id"] == "000003" and issue["code"] == "duplicate_finance_rows"
               for issue in report["issues"])
    assert [row["key"]["item_id"] for row in report["decisions"] if row["ready"]] == (
        ["0001001", "0001002"] if target_known else []
    )


@pytest.mark.parametrize("stage, list_complete", [
    ("poll_order_export", True),
    ("complete_with_missing", False),
])
def test_unfinished_source_is_not_a_completed_export_with_known_missing_orders(
    source_files, stage, list_complete,
):
    capture, evidence = source_files
    capture["capture_complete"] = False
    pending = capture["groups"][1]
    pending["rows"].pop()
    pending.update(stage=stage, list_complete=list_complete, error="来源未完成")
    report = preview_pricing(capture, evidence)
    coverage = next(row for row in report["source_coverage"] if row["source"] == "loss_pending")
    assert not coverage["preparation_complete"]
    assert not any(decision["ready"] for decision in report["decisions"])


def test_capture_finance_status_issues_survive_absent_detail_evidence(source_files):
    capture, _ = source_files
    capture["groups"][1]["rows"].pop()
    capture["groups"][0]["rows"][0]["current_status_matches_source"] = False
    report = preview_pricing(capture)
    assert {issue["code"] for issue in report["issues"] if issue["order_id"] == "000003"} == {
        "missing_order_finance", "missing_order_details",
    }
    assert {issue["code"] for issue in report["issues"] if issue["order_id"] == "000001"} == {
        "order_status_conflict", "missing_order_details",
    }


@pytest.mark.parametrize("last_poll", ["2026-11-09T15:05:00+08:00", "2026-11-10T00:00:01+08:00"])
def test_replay_cannot_predate_export_or_use_a_cross_day_export(source_files, last_poll):
    capture, evidence = source_files
    capture["groups"][0]["batches"][0]["polls"] = [{"status": "success", "queried_at": last_poll}]
    with pytest.raises(ValueError):
        preview_pricing(capture, evidence)


@pytest.mark.parametrize("downloaded_at", ["2026-11-09T15:05:00+08:00", "2026-11-10T00:00:01+08:00"])
def test_replay_checks_download_completion_after_successful_poll(source_files, downloaded_at):
    capture, evidence = source_files
    capture["groups"][0]["batches"][0]["downloaded_at"] = downloaded_at
    with pytest.raises(ValueError, match="评估须在同一北京时间业务日"):
        preview_pricing(capture, evidence)
    report = preview_pricing(capture)
    assert report["latest_capture_observation_at"] == downloaded_at


@pytest.mark.parametrize("defect", ["download_before_success", "reversed_polls"])
def test_finance_timestamps_must_follow_batch_observation_order(source_files, defect):
    capture, evidence = source_files
    batch = capture["groups"][0]["batches"][0]
    if defect == "download_before_success":
        batch["downloaded_at"] = "2026-11-09T14:25:00+08:00"
    else:
        batch["polls"].insert(0, {"status": "running", "queried_at": "2026-11-09T14:28:00+08:00"})

    with pytest.raises(ValueError, match="评估须在同一北京时间业务日"):
        preview_pricing(capture, evidence)
    report = preview_pricing(capture)
    assert "财务导出跨业务日或时点次序异常" in report["global_issues"]


def test_same_main_sku_with_different_reference_sales_does_not_block_siblings(source_files):
    capture, evidence = source_files
    main_sku = evidence["online"][0]["state"]["main_sku"]
    evidence["orders"][1]["lines"][1]["mapping"]["main_sku"] = main_sku
    evidence["online"][1]["state"]["main_sku"] = main_sku
    report = preview_pricing(capture, evidence)
    assert len(report['decisions']) == 2
    assert all(d['ready'] for d in report['decisions'])

def test_known_erp_shop_is_required_to_bind_the_online_mapping(source_files):
    capture, evidence = source_files
    evidence["orders"][0]["lines"][0]["mapping"]["erp_shop"] = "其他ERP店铺"
    report = preview_pricing(capture, evidence)
    assert not any(decision["ready"] for decision in report["decisions"])
    assert any(issue["order_id"] == "000001" and issue["code"] == "mapping_shop_mismatch"
               for issue in report["issues"])


def test_evidence_references_and_versions_are_reported_even_without_fx(source_files):
    capture, evidence = source_files
    evidence["fx"] = None
    report = preview_pricing(capture, evidence)
    assert not report["decisions"]
    assert report["source_versions"]["details"] == "sample-detail-v1"
    assert report["references"]["online"][0]["ref"] == "sample-online-state/0001001"
    assert report["references"]["contracts"]["MY"] == "sample-contract/MY"
    line = report["references"]["lines"]['["000001","01"]']
    assert line["target"]["item_id"] == "0001001"
    assert line["main_sku"] == "SAMPLE-MAIN-1"
    assert line["loss_input"] == {
        "quantity": "2", "amount": "-2.40", "currency": "CNY", "basis": "order",
    }

def test_preview_traces_order_finance_versions_without_using_margin_for_pricing(source_files):
    capture, evidence = source_files
    baseline = preview_pricing(capture, evidence)
    capture["groups"][0]["rows"][0]["fields"]["修正一次毛利"] = "-1.2300"
    capture["groups"][2]["rows"][0]["fields"]["修正一次毛利"] = "0"
    for group in capture["groups"]:
        for row in group["rows"]:
            if row["orderid"] == "000003":
                row["fields"]["修正一次毛利"] = None
    report = preview_pricing(capture, evidence)

    finance = report["references"]["order_finance"]["000001"]
    assert [(row["source"], row["row_number"], row["order_status"], row["corrected_first_margin"])
            for row in finance] == [
        ("loss_allocatable", 1, "可配货", "-1.2300"),
        ("blocked_allocatable", 1, "可配货", "0"),
    ]
    assert report["references"]["order_finance"]["000003"][0]["corrected_first_margin"] is None
    assert report["decisions"] == baseline["decisions"]


def test_candidate_membership_and_parent_hint_order_preserve_price(source_files):
    capture, evidence = source_files
    capture["groups"][0]["rows"][0]["fields"]["item ID"] = "p001,p002"
    baseline = preview_pricing(capture, evidence)

    capture["groups"][0]["rows"][0]["fields"]["item ID"] = "p002,p001"
    blocked = capture["groups"][2]
    blocked["candidate_order_ids"].remove("000001")
    blocked["rows"] = [row for row in blocked["rows"] if row["orderid"] != "000001"]
    blocked["total"] -= 1
    blocked["batches"] = []
    replay = preview_pricing(capture, evidence)

    assert replay["evaluation_complete_sources"] == baseline["evaluation_complete_sources"]
    fields = ("key", "ready", "reason_code", "selected_row_id", "unit_loss", "target_actual", "upload_price")
    assert [[decision.get(field) for field in fields] for decision in replay["decisions"]] == [
        [decision.get(field) for field in fields] for decision in baseline["decisions"]
    ]
    assert len(replay["references"]["order_finance"]["000001"]) == 1


def live_scoped_preparation(capture,evidence):
    from copy import deepcopy
    orders=deepcopy(evidence['orders'][:2]);proofs={}
    for order in orders:
        oid=order['order_id'];order['lines']=order['lines'][:1];line=order['lines'][0]
        amount=next(row['fields']['修正一次毛利'] for group in capture['groups'] for row in group['rows'] if row['orderid']==oid)
        line['detail_id']=oid;line['detail_ref']='native/'+oid
        line['loss'].update(quantity='1',amount=amount,currency='CNY',basis='order',allocation_reliable=True,
            fee_scope='erp-corrected-first-margin-v1',abnormal=False)
        proofs[oid]={'record_kind':'same_sku_order','order_record_id':oid,'native_refs':['physical/'+oid],
            'quantity':'1','erp_sku':'S'+oid,'finance_refs':[{'source':'loss_allocatable','row_index':0}],
            'currency':'CNY','currency_contract':'erp-order-finance-rmb-20261111'}
    orders[1]['lines'][0]['mapping']['key']['shop_id']='OTHER-SHOP'
    orders[1]['lines'][0]['mapping']['erp_shop']='OTHERPH'
    for group in capture['groups']:
        for row in group['rows']:
            if row['orderid']=='000002':
                row['fields']['店铺']='OTHERPH'
    first=orders[0]['lines'][0]['mapping']
    return {'schema_version':'erp-order-loss-preparation-v1','version':'erp-same-sku-order-loss-v1',
        'erp_capture_id':capture['capture_id'],'synthetic':True,'sample_only':False,'pricing_ready':False,
        'observed_at':evidence['evaluated_at'],'orders':orders,'issues':[],'summary':{},'proofs':proofs,
        'shop_scopes':{'000003':{'shop_id':first['key']['shop_id'],'erp_shop':first['erp_shop'],'ref':'headers/000003'}},
        'references':{'currency_contract':'erp-order-finance-rmb-20261111','order_headers':'headers'}}


@pytest.mark.parametrize('has_shop_header', [True, False])
def test_live_missing_native_order_only_skips_that_order(source_files, has_shop_header):
    from app.adapters.erp_pricing import prepare_pricing
    capture,evidence=source_files
    prep=live_scoped_preparation(capture,evidence)
    if not has_shop_header:
        prep['shop_scopes'] = {}
    report=prepare_pricing(capture,order_preparation=prep).report
    assert len(report['evaluation_complete_sources']) == 4
    assert {report['references']['lines'][rid]['order_id'] for rid in report['references']['retained_row_ids']} == {'000001', '000002'}
    assert [(row['order_id'], row['code']) for row in report['issues']] == [('000003', 'missing_order_details')]


def test_live_conflicting_shop_scope_never_localizes_a_missing_order(source_files):
    from app.adapters.erp_pricing import prepare_pricing
    capture,evidence=source_files;prep=live_scoped_preparation(capture,evidence)
    prep['shop_scopes']['000003']['erp_shop']='UNRELATED-NAME'
    report=prepare_pricing(capture,order_preparation=prep).report
    assert 'loss_pending' not in report['evaluation_complete_sources']



def test_positive_evidence_of_conflicting_shop_ids_still_blocks_preparation(source_files):
    from app.adapters.erp_pricing import prepare_pricing
    capture,evidence=source_files;prep=live_scoped_preparation(capture,evidence)
    prep['orders'][0]['lines'][0]['mapping']['key']['shop_id']='SALES-DIFFERENT-ID'
    report=prepare_pricing(capture,order_preparation=prep).report
    assert report['evaluation_complete_sources'] == []



def test_conflicting_shop_ids_cannot_escape_via_an_alias_name(source_files):
    from app.adapters.erp_pricing import prepare_pricing
    capture,evidence=source_files;prep=live_scoped_preparation(capture,evidence)
    prep['orders'][0]['lines'][0]['mapping']['key']['shop_id']='OTHER-SHOP'
    report=prepare_pricing(capture,order_preparation=prep).report
    assert report['evaluation_complete_sources']==[]


def test_live_online_bundle_uses_same_engine_and_cannot_cross_capture(source_files):
    from copy import deepcopy
    from app.adapters.erp_pricing import prepare_pricing
    from app.domain.engine import evaluate
    capture,evidence=source_files;prep=live_scoped_preparation(capture,evidence)
    # 第三单缺原生明细，第一单同店也应进入统一引擎算价。
    online=deepcopy(evidence['online'][:1]);online[0]['state']['fee_scope']='erp-corrected-first-margin-v1'
    bundle={'schema_version':'erp-live-online-preparation-v2','version':'erp-live-online-v2',
        'erp_capture_id':capture['capture_id'],'synthetic':True,'sample_only':False,'pricing_ready':False,
        'observed_at':'2026-11-09T15:00:00+08:00','evaluated_at':'2026-11-09T15:00:00+08:00','online':online,'contracts':evidence['contracts'],
        'issues':[],'references':{'sales_source':'S1'}}
    fx={'date':'2026-11-09','display_units_per_cny':{'CNY':'1','MYR':'0.5'},
        'source_rates':{'CNY':'1','MYR':'2'},'version':'erp-current-config-usage-day-v1','ref':'FX',
        'queried_at':'2026-11-09T15:01:00+08:00','source_effective_date':None,
        'date_policy':'current_config_on_usage_day','synthetic':True}
    prepared=prepare_pricing(capture,current_fx=fx,order_preparation=prep,online_preparation=bundle)
    assert len(prepared.snapshot.online)==1
    decisions=evaluate(prepared.snapshot,prepared.evaluated_at)
    first=next(row for row in decisions if row.key.item_id=='0001001')
    assert first.ready and first.upload_price>Decimal('20')
    bundle['erp_capture_id']='OTHER'
    with pytest.raises(ValueError):
        prepare_pricing(capture,current_fx=fx,order_preparation=prep,online_preparation=bundle)


def test_status_changed_unknown_product_is_skipped_without_holding_its_shop(source_files):
    from app.adapters.erp_pricing import prepare_pricing
    capture,evidence=source_files;prep=live_scoped_preparation(capture,evidence)
    for group in capture['groups']:
        for row in group['rows']:
            if row['orderid']=='000003':
                row['current_status_matches_source']=False
                row['fields']['订单状态']='作废'
    report=prepare_pricing(capture,order_preparation=prep).report
    assert len(report['evaluation_complete_sources'])==4
    assert report['summary']['normalized_detail_lines']==2
    assert any(row['order_id']=='000003' and row['code']=='order_status_conflict' for row in report['issues'])


def test_live_changed_status_excludes_only_that_order_from_same_sku_minimum(source_files):
    from copy import deepcopy
    from app.adapters.erp_pricing import prepare_pricing
    capture, evidence = source_files
    prep = live_scoped_preparation(capture, evidence)
    mapping = prep['orders'][0]['lines'][0]['mapping']
    prep['orders'][1]['lines'][0]['mapping'] = deepcopy(mapping)
    for group in capture['groups']:
        for row in group['rows']:
            if row['orderid'] == '000002':
                row['fields']['店铺'] = mapping['erp_shop']
                row['current_status_matches_source'] = False
    report = prepare_pricing(capture, order_preparation=prep).report
    assert len(report['evaluation_complete_sources']) == 4
    assert [report['references']['lines'][rid]['order_id'] for rid in report['references']['retained_row_ids']] == ['000001']
    assert any(row['code'] == 'order_status_conflict' and row['order_id'] == '000002' for row in report['issues'])


def test_missing_live_mapping_is_local_but_missing_candidate_list_is_not(source_files):
    from app.adapters.erp_pricing import prepare_pricing
    capture, evidence = source_files
    prep = live_scoped_preparation(capture, evidence)
    prep['orders'][0]['lines'][0]['mapping'] = None
    report = prepare_pricing(capture, order_preparation=prep).report
    assert len(report['evaluation_complete_sources']) == 4
    assert any(row['code'] == 'missing_online_mapping' and row['order_id'] == '000001' for row in report['issues'])
    assert [report['references']['lines'][rid]['order_id'] for rid in report['references']['retained_row_ids']] == ['000002']
    next(g for g in capture['groups'] if g['source'] == 'blocked_allocatable')['list_complete'] = False
    report = prepare_pricing(capture, order_preparation=prep).report
    assert 'blocked_allocatable' not in report['evaluation_complete_sources']
    assert any('禁止发货 × 可配货' in reason for reason in report['global_issues'])
