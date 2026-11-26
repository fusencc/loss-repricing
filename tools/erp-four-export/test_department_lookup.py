"""按部门补查已导出的订单，运行前可用 --plan 离线核对范围。"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from math import ceil
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.adapters.erp_bulk_capture import ERP_DEPARTMENTS, _session, _workbook
from app.adapters.erp_order_headers import capture_order_headers
from app.adapters.erp_detail_capture import capture_erp_details
from app.config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="第一步的输出目录（含 result.json）")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    manifest = json.loads((source / "result.json").read_text())
    groups = defaultdict(set)
    for case in manifest["results"]:
        for batch in case["batches"]:
            path = source / Path(batch["file"]).name
            rows, _ = _workbook(path.read_bytes(), ("订单编号", "部门"))
            for row in rows:
                groups[row["部门"]].add(row["订单编号"])

    for dept in ERP_DEPARTMENTS:
        count = len(groups[dept])
        print(f"{dept}：{count} 单，{ceil(count / 1000)} 批", flush=True)
    if args.plan:
        return 0

    settings = Settings(_env_file=ROOT / ".env")
    output = Path(mkdtemp(prefix="lookup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-", dir=source.parent))
    capture_settings = SimpleNamespace(runtime_dir=output, erp_capture_bloc=settings.erp_capture_bloc,
                                       erp_capture_platform_id=settings.erp_capture_platform_id)
    print(f"结果目录：{output}，请求间隔：{settings.erp_capture_request_interval_seconds} 秒", flush=True)
    started = monotonic()
    report = {"source": str(source), "departments": []}

    def progress(message):
        print(message, flush=True)

    with _session(settings) as session:
        for dept in ERP_DEPARTMENTS:
            ids = sorted(groups[dept])
            if not ids:
                continue
            start = monotonic()
            capture = {"capture_id": f"{output.name}-{dept}", "groups": [{"candidate_order_ids": ids}]}
            headers = capture_order_headers(capture_settings, capture, web_session=session, progress=progress)
            middle = monotonic()
            details = capture_erp_details(capture_settings, capture, web_session=session, progress=progress)
            end = monotonic()
            item = {
                "department": dept, "requested": len(ids), "headers_found": len(headers["orders"]),
                "details_found": details["summary"]["observed_orders"],
                "headers_seconds": round(middle - start, 1), "details_seconds": round(end - middle, 1),
                "task_polls": [len(batch["polls"]) for batch in details["batches"]],
                "rate_limit_observed": headers["rate_limit_observed"] or details["rate_limit_observed"],
                "complete": headers["acquisition_complete"] and details["acquisition_complete"],
                "headers_file": headers["artifact_file"], "details_file": details["artifact_file"],
            }
            report["departments"].append(item)
            report["elapsed_seconds"] = round(end - started, 1)
            (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            progress(f"{dept}：店铺归属 {item['headers_found']}/{len(ids)}，商品明细 {item['details_found']}/{len(ids)}，耗时 {end-start:.1f} 秒，记录到限流 {item['rate_limit_observed']}")
    progress(f"总查询耗时 {report['elapsed_seconds']} 秒，汇总文件：{output / 'summary.json'}")
    return 0 if all(item["complete"] for item in report["departments"]) else 1


if __name__ == "__main__":
    sys.exit(main())

