"""运行 python -m app.capture_erp --limit 5，输出到被Git忽略的runtime。

仅运行已核实的正式环境四组候选与逐订单明细读取，不运行价格计算或提交。
"""
import argparse
import json
from datetime import datetime
from app.adapters.erp_capture import capture_erp_sample
from app.config import get_settings


def main():
    parser = argparse.ArgumentParser(description='ERP四组候选只读抽样')
    parser.add_argument('--limit', type=int, choices=range(1, 11), default=5)
    args = parser.parse_args()
    settings = get_settings()
    result = capture_erp_sample(settings, limit=args.limit)
    directory = settings.runtime_dir / 'source-captures'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"erp-sample-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(path)
    for group in result['groups']:
        print(group['kind'], group['status'], 'rows=', len(group['rows']), 'error=', group['error'])
    print('detail_errors=', sum(bool(d['error']) for d in result['details']), 'pricing_ready=False')


if __name__ == '__main__':
    main()
