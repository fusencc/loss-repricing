"""离线验收指定 ERP 归档，清洗及只读预览写入全新目录。"""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter

from app.adapters.base import SourceUnavailable
from app.adapters.erp_bulk_capture import BUSINESS_FIELDS, LOSS_FIELDS, STATUS_LABELS, _workbook, finance_batch_scope
from app.services.erp_capture_store import load_capture, write_verified
from app.services.erp_clean_capture import save_clean_capture
from app.services.erp_pricing import preview_pricing


def check_originals(capture):
    failures = []
    projected_rows = {"loss_pool": 0, "finance": 0}
    original_bytes = file_count = 0
    for group in capture["groups"]:
        source = group["source"]
        files = group.get("files", [])
        # 合成样例可能只有归档投影，报告不计入原件通过数。
        if capture.get("synthetic") and not files:
            continue
        batches = group.get("batches", [])
        numbered = all(type(batch.get("number")) is int and batch["number"] == index
                       for index, batch in enumerate(batches, 1))
        if not numbered:
            failures.append({"source": source, "reason": "finance_batch_number_mismatch"})
        finance_batches = [batch for batch in batches if batch.get("status") in {"complete", "partial"}]
        finance_names = [f"{source}-order-finance-{batch['number']:03d}.xlsx" for batch in finance_batches] if numbered else []
        loss_name = f"{source}-loss-orders.xlsx"
        expected = dict.fromkeys(finance_names, BUSINESS_FIELDS)
        if source.startswith("loss_") and group["list_complete"]:
            expected[loss_name] = LOSS_FIELDS

        registered, projected = set(), {}
        for index, item in enumerate(files, 1):
            file_count += 1
            path = Path(item["file"])
            reason = None
            try:
                content = path.read_bytes()
            except OSError:
                reason = "unreadable"
            else:
                original_bytes += len(content)
                if len(content) != item["bytes"] or sha256(content).hexdigest() != item["sha256"]:
                    reason = "mismatch"
                elif path.name in expected:
                    if path.name in registered:
                        reason = "duplicate_original"
                    else:
                        try:
                            projected[path.name], _ = _workbook(content, expected[path.name])
                        except SourceUnavailable:
                            reason = "invalid_original_workbook"
            registered.add(path.name)
            if reason:
                failures.append({"source": source, "file_index": index, "reason": reason})

        for name in sorted(expected.keys() - registered):
            failures.append({"source": source, "file": name, "reason": "missing_original"})

        if loss_name in projected:
            rows = projected[loss_name]
            ids = list(dict.fromkeys(row["订单编号"] for row in rows))
            if rows != group["loss_rows"] or ids != group["candidate_order_ids"]:
                failures.append({"source": source, "reason": "loss_pool_projection_mismatch"})
            else:
                projected_rows["loss_pool"] += len(rows)
        finance_matches = True
        candidates = set(group["candidate_order_ids"])
        requested = set()
        for batch, name in zip(finance_batches, finance_names):
            if name not in projected:
                continue
            order_ids = batch.get("order_ids")
            scope = None
            if (
                isinstance(order_ids, list) and order_ids
                and all(isinstance(order, str) and order.strip() for order in order_ids)
            ):
                ids = set(order_ids)
                if len(ids) == len(order_ids) and ids <= candidates and not ids & requested:
                    scope = finance_batch_scope(order_ids, [row["订单编号"] for row in projected[name]])
                    requested.update(ids)
            if scope is None or scope["unexpected"] or scope != batch.get("scope_check"):
                finance_matches = False
                failures.append({"source": source, "batch": batch["number"], "reason": "finance_batch_scope_mismatch"})
                continue
            status = "partial" if scope["missing"] or scope["duplicate_rows"] else "complete"
            if batch["status"] != status:
                finance_matches = False
                failures.append({"source": source, "batch": batch["number"], "reason": "finance_batch_status_mismatch"})

        if finance_matches and finance_names and all(name in projected for name in finance_names):
            rows = [
                {
                    "orderid": row["订单编号"], "platform": row["平台"], "source_status": group["status"],
                    "current_status_matches_source": row["订单状态"] == STATUS_LABELS[group["status"]],
                    "fields": row,
                }
                for name in finance_names for row in projected[name]
            ]
            if rows != group["rows"]:
                failures.append({"source": source, "reason": "finance_projection_mismatch"})
            else:
                projected_rows["finance"] += len(rows)
    return {"files": file_count, "bytes_read": original_bytes,
            "projected_rows": projected_rows, "failures": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="尚未创建的验收目录")
    parser.add_argument("--evidence", type=Path, help="绑定指定采集编号的算价证据 JSON")
    parser.add_argument("--erp-details", type=Path, help="ERP 商品明细 XLSX")
    parser.add_argument("--sales-variants", type=Path, help="销售在线变体 XLSX")
    parser.add_argument("capture_id")
    args = parser.parse_args()
    if (args.erp_details is None) != (args.sales_variants is None):
        parser.error("--erp-details 和 --sales-variants 必须成对提供")

    started = perf_counter()
    capture = load_capture(args.source_runtime, args.capture_id)
    archive_seconds = perf_counter() - started
    evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence else None
    exports = (args.erp_details.read_bytes(), args.sales_variants.read_bytes()) if args.erp_details else None
    args.output.mkdir(mode=0o700)
    originals = check_originals(capture)
    started = perf_counter()
    clean = save_clean_capture(args.output, capture)
    clean_seconds = perf_counter() - started
    report = preview_pricing(capture, evidence, exports=exports)
    write_verified(args.output / "preview.json", report)
    summary = {
        "capture_id": args.capture_id,
        "archive_read_seconds": round(archive_seconds, 6),
        "clean_seconds": round(clean_seconds, 6),
        "clean_database_bytes": (args.output / "erp-clean.sqlite3").stat().st_size,
        "originals": originals,
        "clean": clean,
        "preview": report["summary"],
        "preview_issues": dict(Counter(issue["code"] for issue in report["issues"])),
    }
    if exports is not None:
        summary["export_mapping"] = report["export_mapping"]["summary"]
    write_verified(args.output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(originals["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
