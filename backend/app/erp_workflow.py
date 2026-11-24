"""按部门串行运行 ERP 只读链路，非完整结果返回非零退出码。"""
from datetime import date
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from app.adapters.base import SourceUnavailable
from app.config import get_settings
from app.observability import configure_logging, event
from app.services.erp_workflow import run_workflow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roster-date', type=date.fromisoformat, help='仅在该北京时间日期按代码内七人名单采集；日期不符或名单无效停止')
    parser.add_argument('--capture-id', help='显式离线回放该采集；省略则依次处理四个 TikTok 部门')
    parser.add_argument('--evidence', type=Path, help='离线回放的绑定算价证据 JSON')
    parser.add_argument('--erp-details', type=Path)
    parser.add_argument('--sales-variants', type=Path)
    args = parser.parse_args()
    if bool(args.erp_details) != bool(args.sales_variants):
        parser.error('两份明细和在线导出必须成对提供')
    if not args.capture_id and (args.evidence or args.erp_details):
        parser.error('补充文件需要显式指定 --capture-id')
    # 手动测试只读到待提价归档；关闭本进程的提交权限，不改其他进程或数据库中的授权。
    settings = get_settings().model_copy(update={'live_submit_enabled': False})
    configure_logging(settings, role='capture')
    print('本次为只读测试：只计算并保存待提交价格，不会上传，也不会排队等待提交。', file=sys.stderr, flush=True)
    try:
        evidence = json.loads(args.evidence.read_text(encoding='utf-8')) if args.evidence else None
        exports = (args.erp_details.read_bytes(), args.sales_variants.read_bytes()) if args.erp_details else None
        result = run_workflow(settings, capture_id=args.capture_id, evidence=evidence,
                              exports=exports, roster_date=args.roster_date,
                              progress=lambda message: print(message, flush=True))
    except (SourceUnavailable, OSError, ValueError, KeyError, sqlite3.Error) as error:
        error_id = event('erp.failed', exception=error, source='erp', synthetic=False,
                         stage='collect', error_code='workflow_failed')
        print(f'链路运行失败，请按错误编号查日志：{error_id}')
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"本次运行已结束：算价通过 {result['ready_prices']} 条，未提交价格。"
          f"汇总：{Path(result['artifact_dir']) / 'result.json'}", file=sys.stderr)
    for batch in result.get('departments', [result]):
        if batch.get('artifact_dir'):
            print(f"待提交清单：{Path(batch['artifact_dir']) / 'pending-prices.json'}", file=sys.stderr)
    return 0 if result['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
