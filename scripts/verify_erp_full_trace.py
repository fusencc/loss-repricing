import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3

parser = argparse.ArgumentParser(description="核对完整批次的隔离清洗库与缺项预览")
parser.add_argument("replay_dir", type=Path)
root = parser.parse_args().replay_dir
report = json.loads((root / "preview.json").read_text(encoding="utf-8"))
if report["evidence_version"] is not None or report.get('live_preparation_version') is not None or not all(row["finance_complete"] for row in report["source_coverage"]):
    parser.error("此核对要求四类财务完整，且预览未提供补充算价证据")
preview = Counter(
    (order_id, row["source"], row["row_number"], row["order_status"], row["corrected_first_margin"], row["erp_shop"])
    for order_id, rows in report["references"]["order_finance"].items() for row in rows
)
with sqlite3.connect(f"file:{root / 'erp-clean.sqlite3'}?mode=ro", uri=True) as db:
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    rows = db.execute("""
        SELECT o.order_id, o.source, o.row_number, v.fields_json
        FROM observations o JOIN order_versions v USING(capture_id, order_id, version)
        WHERE o.capture_id = ?
        """, (report["capture_id"],))
    cleaned = Counter(
        (order_id, source, number, fields.get("订单状态"), fields.get("修正一次毛利"), fields.get("店铺"))
        for order_id, source, number, payload in rows for fields in (json.loads(payload),)
    )
assert preview == cleaned and sum(cleaned.values()) == report["summary"]["source_rows"]
assert report["execution_allowed"] is False
assert set(report["references"]["order_finance"]) == {issue["order_id"] for issue in report["issues"]}
print("clean-to-preview observation parity:", sum(cleaned.values()))
print("order issue coverage:", len(report["references"]["order_finance"]))
