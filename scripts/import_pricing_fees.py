"""显式导入用户固定表；不修改Excel、不请求外部接口、不启用真实提交。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.adapters.fixed_pricing_fees import import_workbook
from app.services.erp_capture_store import write_verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workbook', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = import_workbook(args.workbook)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_verified(args.output, config)
    print(json.dumps({'version': config['version'], 'sites': sorted(config['overseas']),
                     'departments': len(config['departments']), 'product_inputs': len(config['product_inputs'])}))


if __name__ == '__main__':
    main()
