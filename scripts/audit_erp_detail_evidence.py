"""核对已保存 ERP 明细探针、导出与预览的身份和金额证据。"""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import json
from pathlib import Path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def line_key(order, sku, quantity):
    return order, sku, Decimal(str(quantity))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-dir", type=Path, required=True)
    parser.add_argument("--preview", type=Path, required=True)
    args = parser.parse_args()
    raw = read_json(args.probe_dir / "sku-readonly-probe.json")
    details = read_json(args.probe_dir / "sku-erp-detail-export-probe.json")
    crosscheck = read_json(args.probe_dir / "sku-erp-id-crosscheck.json")
    report = read_json(args.preview)
    mapped = report["export_mapping"]["rows"]
    expected = Counter(line_key(row["订单编号"], row["商品SKU列表"], row["订单数量"]) for row in details)
    raw_lines = [(order["order"], row) for order in raw for row in order["rows"]]
    observed = Counter(line_key(order, row["sku"], row["qty"]) for order, row in raw_lines)
    preview = Counter(line_key(row["order_id"], row["erp_sku"], row["quantity"]) for row in mapped)
    if observed != expected or preview != expected:
        parser.exit(1, "探针、导出及预览的订单、SKU 或件数多重集合不一致\n")

    orders = defaultdict(list)
    for row in details:
        orders[row["订单编号"]].append(row)
    repeated_transactions = sum(
        len(rows) > 1 and len({row["交易号"] for row in rows}) < len(rows)
        for rows in orders.values()
    )
    summary = {
        "orders": len(orders),
        "lines_reconciled_by_order_sku_quantity": len(details),
        "multi_line_orders": sum(len(rows) > 1 for rows in orders.values()),
        "orders_where_export_transaction_is_not_unique": repeated_transactions,
        "raw_id_present_rows": sum(row["id"] not in ("", None) for _, row in raw_lines),
        "raw_currency_present_rows": sum(row["currency"] not in ("", None) for _, row in raw_lines),
        "raw_adjusted_margin_present_rows": sum(row["adjustGrossProfit"] not in ("", None) for _, row in raw_lines),
        "export_sku_adjusted_margin_present_rows": sum(row["SKU修正毛利"] not in ("", None) for row in details),
        "mapped_rows": sum(row["target"] is not None for row in mapped),
        "two_interface_transaction_id_sample_sizes": [
            check["lines"] for check in crosscheck if check["finance_transaction_unique"]
            and check["detail_nonempty"]["transactionid"] == check["finance_nonempty"]["transactionid"] == check["lines"]
        ],
        "ready_prices": report["summary"]["ready_prices"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
