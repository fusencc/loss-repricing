import json
import sqlite3

from app.services import erp_capture_store as store


def test_bulk_archive_and_clean_orders_preserve_sources_and_conflicts(tmp_path):
    folder = tmp_path / "source-captures" / "erp-bulk-20261110T000000Z-0123456789abcdef0123456789abcdef"
    path = folder / "result.json"
    order = "00123456789012345678"

    def group(source, items, margin, status):
        fields = {"订单编号": order, "item ID": items, "修正一次毛利": margin,
                  "订单状态": status, "货运单号": None}
        return {"source": source, "rows": [{"orderid": order, "fields": fields}],
                "candidate_order_ids": [order], "missing_order_ids": []}

    result = {"acquisition_mode": "bulk_download", "queried_at": "2026-11-10T00:00:00+00:00",
              "capture_complete": True, "details": [], "groups": [
                  group("loss_allocatable", "002,001,001", "-1.2300", "可配货"),
                  group("blocked_allocatable", "001；002", "-1.2300", "可配货"),
                  group("blocked_pending", "001,002", "0", "待处理"),
              ]}
    store.write_verified(path, result)
    saved = store.save_capture(tmp_path, {**result, "result_file": str(path)})
    assert saved["cleaning_status"] == "saved"
    capture_id = saved["capture_id"]
    restored = store.load_capture(tmp_path, capture_id)
    assert restored["groups"] == result["groups"]
    assert not path.exists()
    assert (folder / "result.json.gz").is_file()
    manifest = json.loads((folder.parent / (capture_id + ".json")).read_text())
    assert "groups" not in manifest

    store.rebuild_clean_capture(tmp_path, capture_id)
    with sqlite3.connect(tmp_path / "erp-clean.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM observations").fetchone() == (3,)
        versions = [json.loads(row[0]) for row in db.execute("SELECT fields_json FROM order_versions")]
        assert len(versions) == 2
        assert {v["修正一次毛利"] for v in versions} == {"-1.2300", "0"}
        assert all(v["item ID"] == ["001", "002"] and v["货运单号"] is None for v in versions)
        assert db.execute("SELECT code FROM issues").fetchall() == [("conflicting_finance",)]

def test_clean_failure_leaves_archive_queryable(tmp_path, monkeypatch):
    def fail(*args):
        raise sqlite3.OperationalError("database busy")

    folder = tmp_path / "source-captures" / "erp-bulk-20261110T000000Z-0123456789abcdef0123456789abcdef"
    result = {"acquisition_mode": "bulk_download", "queried_at": "2026-11-10T00:00:00+00:00",
              "capture_complete": False, "groups": [], "result_file": str(folder / "result.json")}
    store.write_verified(folder / "result.json", result)
    with monkeypatch.context() as patch:
        patch.setattr(store, "save_clean_capture", fail)
        saved = store.save_capture(tmp_path, result)
    assert saved["storage_verified"] and saved["cleaning_status"] == "failed"
    assert store.load_capture(tmp_path, saved["capture_id"])["capture_complete"] is False
    assert store.rebuild_clean_capture(tmp_path, saved["capture_id"])["orders"] == 0

def test_rebuild_distinguishes_unread_source_incomplete_list_and_missing_finance(tmp_path):
    capture = {
        "acquisition_mode": "bulk_download",
        "queried_at": "2026-11-10T00:00:00+00:00",
        "capture_complete": False,
        "groups": [
            {"source": "loss_allocatable", "stage": "not_attempted", "list_complete": False, "rows": []},
            {"source": "blocked_pending", "stage": "candidate_list", "list_complete": False,
             "failed_pages": [2], "rows": []},
            {"source": "loss_pending", "stage": "complete_with_missing", "list_complete": True,
             "rows": [], "candidate_order_ids": ["000123"], "missing_order_ids": ["000123"]},
        ],
    }
    saved = store.save_capture(tmp_path, capture)
    store.rebuild_clean_capture(tmp_path, saved["capture_id"])
    with sqlite3.connect(tmp_path / "erp-clean.sqlite3") as db:
        assert set(db.execute("SELECT source, order_id, code FROM issues")) == {
            ("loss_allocatable", "", "source_not_attempted"),
            ("blocked_pending", "", "candidate_list_incomplete"),
            ("loss_pending", "000123", "missing_finance"),
        }
        assert db.execute("SELECT order_id, version FROM observations").fetchall() == [("000123", None)]
