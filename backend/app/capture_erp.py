"""ERP数据获取命令行入口（当前路径核实见docs/erp-source-capture.md）。

默认：erp_bulk_capture.capture_erp_bulk，TikTok四类候选批量下载。
--capture-id：仅从当前runtime_dir读取历史文件，不登录ERP、不重新取数。
本入口保存采集证据，不生成定价Snapshot，不提交调价；汇率另走erp_rates。
"""
import argparse
import json
from sqlite3 import Error as SQLiteError
from uuid import uuid4
from functools import partial
from app.observability import configure_logging, bind_context, event, valid_id

event = partial(event, source='erp', synthetic=False)

from app.adapters.base import SourceUnavailable
from app.adapters.erp_bulk_capture import SOURCES, capture_erp_bulk
from app.config import get_settings
from app.services.erp_capture_store import find_order, load_capture, rebuild_clean_capture, save_capture


def main():
    parser = argparse.ArgumentParser(description='ERP批量下载与保存（默认四类候选）')
    parser.add_argument('--capture-id', help='读取已保存的采集编号，不请求ERP')
    parser.add_argument('--source', choices=SOURCES, help='仅抓指定来源；默认抓四类')
    parser.add_argument('--salesperson', help='ERP销售员姓名；不指定则不筛选销售员')
    parser.add_argument('--max-orders', type=int, help='小量试抓：仅限制财务导出订单数，每类保留完整候选名单')
    parser.add_argument('--batch-size', type=int, help='每批订单数（1至1000，默认1000）')
    parser.add_argument('--progress', action='store_true', help='显示下载进度')
    parser.add_argument('--request-interval', type=float, help='请求间隔秒数；默认读取配置，当前默认16秒')
    parser.add_argument('--order', help='与--capture-id一起使用，按订单号查列表及明细')
    parser.add_argument('--rebuild-clean', action='store_true', help='按采集编号离线重建清洗结果')
    args = parser.parse_args()
    if args.rebuild_clean and (not args.capture_id or args.order):
        parser.error('--rebuild-clean需要--capture-id且不能与--order同用')
    if args.order and not args.capture_id:
        parser.error('--order需要--capture-id')
    if args.max_orders is not None and args.max_orders < 1:
        parser.error('--max-orders必须为正整数')
    if args.batch_size is not None and not 1 <= args.batch_size <= 1000:
        parser.error('--batch-size须在1至1000之间')
    if args.capture_id and any(value is not None for value in (
        args.source, args.salesperson, args.max_orders, args.batch_size, args.request_interval,
    )):
        parser.error('读取归档时不能指定采集参数')
    if args.capture_id and args.progress:
        parser.error('--progress仅用于批量采集')
    settings = get_settings()
    if args.request_interval is not None:
        if not 0 <= args.request_interval <= 300:
            parser.error('--request-interval须在0至300秒之间')
        settings = settings.model_copy(update={'erp_capture_request_interval_seconds': args.request_interval})
    configure_logging(settings, role='capture')
    context = bind_context(operation_id=uuid4().hex)
    context.__enter__()
    event('erp.started', stage='read' if args.capture_id else 'collect')
    try:
        if args.capture_id:
            if args.rebuild_clean:
                counts = rebuild_clean_capture(settings.runtime_dir, args.capture_id)
                event('capture.cleaned', capture_id=args.capture_id, stage='save', count=counts['orders'])
                print(json.dumps(counts, ensure_ascii=False))
                return
            data = load_capture(settings.runtime_dir, args.capture_id)
            print(json.dumps(find_order(data, args.order) if args.order else data,
                             ensure_ascii=False, indent=2))
            event('erp.completed', stage='read')
            return
        result = capture_erp_bulk(
            settings, source=args.source, salesperson=args.salesperson, max_orders=args.max_orders,
            export_batch_size=args.batch_size if args.batch_size is not None else 1000,
            progress=(lambda message: print(message, flush=True)) if args.progress else None,
            event_callback=event,
        )
        saved = save_capture(settings.runtime_dir, result)
        path = settings.runtime_dir / 'source-captures' / (saved['capture_id'] + '.json')
        print('capture_id=', saved['capture_id'], 'storage_verified=True')
        if 'cleaning_status' in saved:
            print('cleaning_status=', saved['cleaning_status'])
        print(path)
        for group in result['groups']:
            print(group['kind'], group['status'], 'total=', group.get('total'),
                  'rows=', len(group['rows']),
                  'stop=', group.get('stop_reason'), 'error=', group['error'])
        print('rate_limit_observed=', result.get('rate_limit_observed', False))
        print('summary=', json.dumps(result.get('summary', {}), ensure_ascii=False))
        print('capture_complete=', result.get('capture_complete', False), 'pricing_ready=False')
        print('permission_scope_verified=False；取数完成不代表业务时点或账号范围已验收')
        print('sample_only=', result.get('sample_only', False), 'selection_complete=', result.get('selection_complete', False))
        successful = result.get('selection_complete', False) if result.get('sample_only') else result.get('capture_complete', False)
        event('erp.completed', stage='collect', complete=bool(successful),
              limited=bool(result.get('rate_limit_observed', False)))
        if not successful:
            parser.exit(1, '批量采集未完成，失败及已成功部分已保存。\n')
    except (SourceUnavailable, OSError, ValueError, KeyError, SQLiteError) as error:
        error_id = getattr(error, '_erp_log_error_id', None)
        if not valid_id(error_id):
            error_id = event('erp.failed', exception=error, capture_id=args.capture_id,
                             stage='read' if args.capture_id else 'collect',
                             error_code='source_unavailable' if isinstance(error, SourceUnavailable) else 'storage_failed')
        # 终端错误只显示静态说明及错误编号，不拼接外部异常文本。
        if isinstance(error, SourceUnavailable):
            parser.exit(1, f'ERP读取失败，请按错误编号查询日志：{error_id}\n')
        parser.exit(1, f'采集文件保存/读取失败或未找到订单，错误编号：{error_id}\n')
    finally:
        context.__exit__(None, None, None)


if __name__ == '__main__':
    main()
