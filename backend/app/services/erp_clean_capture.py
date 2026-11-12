"""从已归档的 ERP 批量结果重建订单级对账数据。"""
from collections import defaultdict
import json
from pathlib import Path
import re
import sqlite3


CLEAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    capture_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    queried_at TEXT NOT NULL,
    source_version TEXT,
    capture_complete INTEGER NOT NULL,
    sample_only INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    capture_id TEXT NOT NULL REFERENCES captures ON DELETE CASCADE,
    order_id TEXT NOT NULL,
    PRIMARY KEY (capture_id, order_id)
);
CREATE INDEX IF NOT EXISTS clean_order_lookup ON orders (order_id, capture_id);
CREATE TABLE IF NOT EXISTS order_versions (
    capture_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    fields_json TEXT NOT NULL,
    PRIMARY KEY (capture_id, order_id, version),
    FOREIGN KEY (capture_id, order_id) REFERENCES orders ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS observations (
    capture_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    source TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    version INTEGER,
    PRIMARY KEY (capture_id, order_id, source, row_number),
    FOREIGN KEY (capture_id, order_id) REFERENCES orders ON DELETE CASCADE,
    FOREIGN KEY (capture_id, order_id, version) REFERENCES order_versions ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS issues (
    capture_id TEXT NOT NULL REFERENCES captures ON DELETE CASCADE,
    source TEXT NOT NULL,
    order_id TEXT NOT NULL,
    code TEXT NOT NULL,
    PRIMARY KEY (capture_id, source, order_id, code)
);
"""


def _fields_json(fields):
    fields = dict(fields)
    item_ids = fields.get("item ID")
    # 财务订单表提供父产品候选集合。原值仍可通过采集编号、来源及行号回查。
    if isinstance(item_ids, str):
        fields["item ID"] = sorted(set(filter(None, re.split(r"[,，;；\s]+", item_ids))))
    # 仅存储 ERP 提供的金额及空值，不复算或分摊到商品明细。
    return json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def save_clean_capture(runtime_dir, capture):
    if capture.get("acquisition_mode") != "bulk_download":
        raise ValueError("清洗只支持 ERP 批量采集")
    capture_id = capture["capture_id"]
    orders = defaultdict(dict)
    observations = []
    issues = set()
    for group in capture["groups"]:
        source = group["source"]
        returned = set()
        for number, row in enumerate(group["rows"], 1):
            order_id = row["orderid"]
            if order_id in returned:
                issues.add((source, order_id, "duplicate_finance_row"))
            returned.add(order_id)
            versions = orders[order_id]
            payload = _fields_json(row["fields"])
            version = versions.setdefault(payload, len(versions) + 1)
            observations.append((order_id, source, number, version))
            if row.get("current_status_matches_source") is False:
                issues.add((source, order_id, "status_discrepancy"))
        for order_id in dict.fromkeys(group.get("candidate_order_ids", [])):
            orders[order_id]
            if order_id not in returned:
                observations.append((order_id, source, 0, None))
        issues.update((source, order_id, "missing_finance") for order_id in group.get("missing_order_ids", []))
        if group.get("error"):
            issues.add((source, "", "source_incomplete"))
        # 来源级缺项保留在问题记录中，避免把未读取的类别理解为零候选。
        if group.get("stage") == "not_attempted":
            issues.add((source, "", "source_not_attempted"))
        elif group.get("list_complete") is False or group.get("failed_pages"):
            issues.add((source, "", "candidate_list_incomplete"))
    issues.update(("", order_id, "conflicting_finance") for order_id, versions in orders.items() if len(versions) > 1)

    db = sqlite3.connect(Path(runtime_dir) / "erp-clean.sqlite3", timeout=15)
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(CLEAN_SCHEMA)
        with db:
            # 以采集编号为单位原子重建。失败时原有结果仍可用。
            db.execute("DELETE FROM captures WHERE capture_id = ?", (capture_id,))
            db.execute("INSERT INTO captures VALUES (?, 1, ?, ?, ?, ?)", (
                capture_id, capture["queried_at"], capture.get("source_version"),
                bool(capture["capture_complete"]), bool(capture.get("sample_only")),
            ))
            db.executemany("INSERT INTO orders VALUES (?, ?)", ((capture_id, order) for order in orders))
            db.executemany("INSERT INTO order_versions VALUES (?, ?, ?, ?)", (
                (capture_id, order, version, payload)
                for order, versions in orders.items() for payload, version in versions.items()
            ))
            db.executemany("INSERT INTO observations VALUES (?, ?, ?, ?, ?)", (
                (capture_id, *row) for row in observations
            ))
            db.executemany("INSERT INTO issues VALUES (?, ?, ?, ?)", (
                (capture_id, *issue) for issue in sorted(issues)
            ))
    finally:
        db.close()
    return {"orders": len(orders), "observations": len(observations),
            "versions": sum(map(len, orders.values())), "issues": len(issues)}
