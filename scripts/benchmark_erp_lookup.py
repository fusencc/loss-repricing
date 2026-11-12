"""对同一批次比较整批读后筛单与进程内重复回查。"""
import argparse
from pathlib import Path
from statistics import median
from time import perf_counter

from app.services.erp_capture_store import find_order, find_saved_order, load_capture


def measure(operation, orders, repeats):
    samples = []
    for order_id in orders * repeats:
        start = perf_counter()
        operation(order_id)
        samples.append((perf_counter() - start) * 1000)
    return round(median(samples), 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("capture_id")
    args = parser.parse_args()
    capture = load_capture(args.runtime, args.capture_id)
    ids = sorted({row["orderid"] for group in capture["groups"] for row in group["rows"]})
    if not ids:
        parser.error("批次需要至少一条财务行")
    orders = list(dict.fromkeys(ids[index] for index in (0, len(ids) // 2, -1)))

    def cached(order_id):
        return find_saved_order(args.runtime, args.capture_id, order_id)

    def uncached(order_id):
        return find_order(load_capture(args.runtime, args.capture_id), order_id)

    start = perf_counter()
    first = cached(orders[0])
    cold_ms = round((perf_counter() - start) * 1000, 3)
    assert first == find_order(capture, orders[0])
    for order_id in orders[1:]:
        assert cached(order_id) == find_order(capture, order_id)
    print({
        "orders_checked": len(orders),
        "first_lookup_ms": cold_ms,
        "uncached_median_ms": measure(uncached, orders, 5),
        "repeat_lookup_median_ms": measure(cached, orders, 5),
    })


if __name__ == "__main__":
    main()
