#!/usr/bin/env python3
"""按当前 ERP 批量采集合同，手动指定来源类型与销售员取数。

实时进度同时输出到终端和本轮 output 目录的 progress.log；
候选名单、分批财务原件与 result.json 也落在同一目录，可边跑边查看。
只下载与归档，不提交调价。凭据读取项目根目录 .env（LR_ERP_USERNAME / LR_ERP_PASSWORD）。

运行示例见本文件 --help，或仓库 docs/erp-source-capture.md。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.adapters.base import SourceUnavailable  # noqa: E402
from app.adapters.erp_bulk_capture import SOURCES, capture_erp_bulk  # noqa: E402
from app.config import get_settings  # noqa: E402

SOURCE_LABELS = {
    'loss_allocatable': '亏损订单 · 可配货（财务复核待处理）',
    'loss_pending': '亏损订单 · 待处理（财务复核待处理）',
    'blocked_allocatable': '禁止发货 · 可配货',
    'blocked_pending': '禁止发货 · 待处理',
}


def _beijing_stamp():
    return datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')


def _utc_run_id():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:12]


class LiveReporter:
    """把进度写到终端，并同步追加到 progress.log；有检查点时打印可读摘要。"""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.log_path = run_dir / 'progress.log'
        self._last_snapshot = None
        self.log_path.write_text('', encoding='utf-8')

    def emit(self, message: str):
        line = f'[{_beijing_stamp()}] {message}'
        print(line, flush=True)
        with self.log_path.open('a', encoding='utf-8') as stream:
            stream.write(line + '\n')
        self._maybe_snapshot()

    def _result_files(self):
        root = self.run_dir / 'source-captures'
        if not root.is_dir():
            return []
        return sorted(root.glob('erp-bulk-*/result.json'), key=lambda path: path.stat().st_mtime)

    def _maybe_snapshot(self):
        files = self._result_files()
        if not files:
            return
        path = files[-1]
        key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        if key == self._last_snapshot:
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return
        self._last_snapshot = key
        self._write_snapshot(path, data)

    def _write_snapshot(self, path: Path, data: dict):
        lines = [
            f'--- 实时检查点 {path.parent.name} ---',
            f"queried_at={data.get('queried_at')} sample_only={data.get('sample_only')}",
        ]
        if data.get('salesperson'):
            person = data['salesperson']
            lines.append(f"销售员确认：{person.get('name')} / id={person.get('id')}")
        for group in data.get('groups') or []:
            batches = group.get('batches') or []
            done = sum(1 for batch in batches if batch.get('status') in ('complete', 'partial', 'failed'))
            lines.append(
                f"{group.get('source')}: stage={group.get('stage')} "
                f"候选={len(group.get('candidate_order_ids') or [])} "
                f"本轮导出={group.get('selected_orders')} "
                f"财务行={len(group.get('rows') or [])} "
                f"缺失={len(group.get('missing_order_ids') or [])} "
                f"批次={done}/{len(batches)} "
                f"error={group.get('error')}"
            )
            for batch in batches:
                if batch.get('status') in ('not_attempted', 'submitting', 'polling'):
                    continue
                scope = batch.get('scope_check') or {}
                sample = (batch.get('order_ids') or [])[:5]
                lines.append(
                    f"  批{batch.get('number')}: status={batch.get('status')} "
                    f"请求={scope.get('requested')} 唯一返回={scope.get('downloaded_unique')} "
                    f"缺失={len(scope.get('missing') or [])} "
                    f"样例订单={sample}"
                )
            for item in group.get('files') or []:
                lines.append(f"  文件: {item.get('file')} sha256={str(item.get('sha256') or '')[:12]}…")
            comparison = group.get('comparison_file')
            if comparison:
                lines.append(f"  核对表: {comparison.get('file')}")
        summary = data.get('summary')
        if summary:
            lines.append(f"summary={json.dumps(summary, ensure_ascii=False)}")
        text = '\n'.join(lines)
        print(text, flush=True)
        with self.log_path.open('a', encoding='utf-8') as stream:
            stream.write(text + '\n')
        snapshot_path = self.run_dir / 'latest-snapshot.json'
        snapshot_path.write_text(json.dumps({
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'result_file': str(path),
            'capture_complete': data.get('capture_complete'),
            'selection_complete': data.get('selection_complete'),
            'stopped_by_rate_limit': data.get('stopped_by_rate_limit'),
            'salesperson': data.get('salesperson'),
            'summary': summary,
            'groups': [
                {
                    'source': group.get('source'),
                    'stage': group.get('stage'),
                    'candidates': len(group.get('candidate_order_ids') or []),
                    'selected_orders': group.get('selected_orders'),
                    'finance_rows': len(group.get('rows') or []),
                    'missing': len(group.get('missing_order_ids') or []),
                    'error': group.get('error'),
                    'files': [item.get('file') for item in (group.get('files') or [])],
                    'comparison_file': (group.get('comparison_file') or {}).get('file'),
                }
                for group in (data.get('groups') or [])
            ],
        }, ensure_ascii=False, indent=2), encoding='utf-8')


def build_parser():
    epilog = '\n'.join([
        '可选来源 --source：',
        *[f'  {name:22} {label}' for name, label in SOURCE_LABELS.items()],
        '',
        '说明：',
        '  · 默认抓四类来源；--salesperson 按销售员筛选所选来源的候选名单和财务明细。',
        '  · 默认每类只导出前 20 笔财务（试抓）；加 --all 才全量。',
        '  · 凭据来自项目 .env，不会写入 output。',
        '',
        '示例：',
        '  PYTHONPATH=backend .venv/bin/python scripts/run_erp_capture.py --list-sources',
        '  PYTHONPATH=backend .venv/bin/python scripts/run_erp_capture.py \\',
        '      --source loss_allocatable --salesperson 杨颖',
        '  PYTHONPATH=backend .venv/bin/python scripts/run_erp_capture.py \\',
        '      --salesperson 杨颖 --all',
    ])
    parser = argparse.ArgumentParser(
        description='ERP 只读采集：可指定来源类型与销售员，实时进度写入终端和 output',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    parser.add_argument('--list-sources', action='store_true', help='列出可选来源后退出，不登录')
    parser.add_argument('--source', choices=sorted(SOURCES), help='只抓这一类；默认四类')
    parser.add_argument('--salesperson', help='ERP 销售员姓名，精确匹配下拉 userName；例如杨颖')
    parser.add_argument('--output', type=Path, default=PROJECT_ROOT / 'output',
                        help='输出根目录，默认项目根下 output/；每轮新建子目录')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--max-orders', type=int, default=20,
                      help='每类财务导出上限，默认 20；仍保存完整候选名单')
    mode.add_argument('--all', action='store_true', help='每类全量导出财务，可能较久')
    parser.add_argument('--batch-size', type=int, default=1000, help='每批订单数，1–1000，默认 1000')
    parser.add_argument('--request-interval', type=float,
                        help='请求间隔秒数；默认读配置，当前通常 16 秒')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_sources:
        for name, label in SOURCE_LABELS.items():
            kind, status = SOURCES[name]
            print(f'{name}\t{label}\tkind={kind}\tstatus={status}')
        return 0
    if args.max_orders is not None and args.max_orders < 1:
        parser.error('--max-orders 必须为正整数')
    if not 1 <= args.batch_size <= 1000:
        parser.error('--batch-size 须在 1 至 1000 之间')
    if args.salesperson is not None and not args.salesperson.strip():
        parser.error('--salesperson 不能为空')
    if args.request_interval is not None and not 0 <= args.request_interval <= 300:
        parser.error('--request-interval 须在 0 至 300 秒之间')

    settings = get_settings()
    if not settings.erp_username or not settings.erp_password.get_secret_value():
        parser.error('缺少 LR_ERP_USERNAME / LR_ERP_PASSWORD，请先在项目根 .env 配置')
    if args.request_interval is not None:
        settings = settings.model_copy(update={'erp_capture_request_interval_seconds': args.request_interval})

    run_id = _utc_run_id()
    run_dir = (args.output.resolve() / f'erp-capture-{run_id}')
    run_dir.mkdir(parents=True, mode=0o700)
    settings = settings.model_copy(update={'runtime_dir': run_dir})

    selected = [args.source] if args.source else list(SOURCES)
    meta = {
        'run_id': run_id,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'sources': selected,
        'source_labels': {name: SOURCE_LABELS[name] for name in selected},
        'salesperson': args.salesperson.strip() if args.salesperson else None,
        'max_orders': None if args.all else args.max_orders,
        'batch_size': args.batch_size,
        'platform': settings.erp_capture_platform,
        'bloc': settings.erp_capture_bloc,
        'output_dir': str(run_dir),
        'note': '只读采集，不提交调价；完成不等于具备提价资格',
    }
    (run_dir / 'run.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

    reporter = LiveReporter(run_dir)
    reporter.emit(f"本轮输出目录：{run_dir}")
    reporter.emit(f"平台/集团：{settings.erp_capture_platform} / {settings.erp_capture_bloc}")
    reporter.emit('来源：' + '、'.join(f'{name}（{SOURCE_LABELS[name]}）' for name in selected))
    reporter.emit('销售员：' + (meta['salesperson'] or '不限'))
    reporter.emit('模式：' + ('全量财务导出' if args.all else f'试抓每类前 {args.max_orders} 笔财务'))
    reporter.emit('进度文件：' + str(reporter.log_path))

    try:
        result = capture_erp_bulk(
            settings,
            source=args.source,
            salesperson=args.salesperson.strip() if args.salesperson else None,
            max_orders=None if args.all else args.max_orders,
            export_batch_size=args.batch_size,
            progress=reporter.emit,
        )
    except (SourceUnavailable, OSError, ValueError, KeyError) as error:
        reporter.emit(f'采集失败：{type(error).__name__}')
        (run_dir / 'failure.json').write_text(
            json.dumps({'error_type': type(error).__name__, 'finished_at': datetime.now(timezone.utc).isoformat()},
                       ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        print(f'失败详情见 {run_dir}；异常类型已写入 failure.json（不含外部原文）。', file=sys.stderr)
        return 1

    reporter._maybe_snapshot()
    ok = result['selection_complete'] if result.get('sample_only') else result.get('capture_complete', False)
    finished = {
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'result_file': result.get('result_file'),
        'capture_complete': result.get('capture_complete'),
        'selection_complete': result.get('selection_complete'),
        'sample_only': result.get('sample_only'),
        'stopped_by_rate_limit': result.get('stopped_by_rate_limit'),
        'salesperson': result.get('salesperson'),
        'summary': result.get('summary'),
        'groups': [
            {
                'source': group.get('source'),
                'stage': group.get('stage'),
                'total': group.get('total'),
                'candidates': len(group.get('candidate_order_ids') or []),
                'selected_orders': group.get('selected_orders'),
                'finance_rows': len(group.get('rows') or []),
                'missing': len(group.get('missing_order_ids') or []),
                'error': group.get('error'),
                'files': group.get('files'),
                'comparison_file': group.get('comparison_file'),
            }
            for group in result.get('groups') or []
        ],
        'success': bool(ok),
    }
    (run_dir / 'summary.json').write_text(json.dumps(finished, ensure_ascii=False, indent=2), encoding='utf-8')
    reporter.emit(f"result.json：{result.get('result_file')}")
    reporter.emit(f"summary.json：{run_dir / 'summary.json'}")
    reporter.emit(f"本轮选择范围完成={ok}；整池完成={result.get('capture_complete')}；"
                  f"限流停止={result.get('stopped_by_rate_limit')}")
    for group in result.get('groups') or []:
        reporter.emit(
            f"结束 {group.get('source')}: rows={len(group.get('rows') or [])} "
            f"stop/error={group.get('error')}"
        )
        if group.get('comparison_file'):
            reporter.emit(f"  订单核对表：{group['comparison_file'].get('file')}")
    print(f'\n查看实时日志：{reporter.log_path}', flush=True)
    print(f'查看本轮目录：{run_dir}', flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\n已取消；已完成批次保留在本轮 output 目录，不会自动重提导出任务。', flush=True)
        raise SystemExit(130)
