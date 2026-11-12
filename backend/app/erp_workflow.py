"""单次调用自主运行 ERP 只读六步链路，非完整结果返回非零退出码。"""
import argparse
import json
from pathlib import Path
import sqlite3

from app.adapters.base import SourceUnavailable
from app.config import get_settings
from app.observability import configure_logging, event
from app.services.erp_workflow import run_workflow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-id', help='显式离线回放该采集；省略则实时抓取四类')
    parser.add_argument('--evidence', type=Path, help='离线回放的绑定算价证据 JSON')
    parser.add_argument('--erp-details', type=Path)
    parser.add_argument('--sales-variants', type=Path)
    args = parser.parse_args()
    if bool(args.erp_details) != bool(args.sales_variants):
        parser.error('两份明细和在线导出必须成对提供')
    if not args.capture_id and (args.evidence or args.erp_details):
        parser.error('补充文件需要显式指定 --capture-id')
    settings = get_settings()
    configure_logging(settings, role='capture')
    try:
        evidence = json.loads(args.evidence.read_text(encoding='utf-8')) if args.evidence else None
        exports = (args.erp_details.read_bytes(), args.sales_variants.read_bytes()) if args.erp_details else None
        result = run_workflow(settings, capture_id=args.capture_id, evidence=evidence,
                              exports=exports, progress=lambda message: print(message, flush=True))
    except (SourceUnavailable, OSError, ValueError, KeyError, sqlite3.Error) as error:
        error_id = event('erp.failed', exception=error, source='erp', synthetic=False,
                         stage='collect', error_code='workflow_failed')
        print(f'链路运行失败，请按错误编号查日志：{error_id}')
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
