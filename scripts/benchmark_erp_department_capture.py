#!/usr/bin/env python3
"""按部门手动下载 ERP 四类订单，独立保存原件、计数与耗时；不算价、不上传。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
import warnings

import httpx
from dotenv import dotenv_values
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.adapters.erp_bulk_capture import ERP_DEPARTMENTS, SOURCES  # noqa: E402
from app.adapters.erp_web import BLOCKED_LIST_RETRY_DELAYS, WEB_BASE, retry_erp_read  # noqa: E402

LOGIN = '/newIp/system/login'
LOSS = '/Api/deficit/dowDeficitOrder'
BLOCKED = '/Api/order/list'
EXPORT = '/newIp/report/orderDownload/asyncOrderListDownload'
TASKS = '/newIp/report/download/list'


class CaptureError(Exception):
    """仅包含可写入报告的固定错误描述。"""


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def load_config(env_file=None):
    # 不使用 Settings 的上级 .env 搜索或生产数据库配置校验。
    values = {**dotenv_values(env_file or ROOT / '.env'), **os.environ}
    return {
        'username': values.get('LR_ERP_USERNAME') or '',
        'password': values.get('LR_ERP_PASSWORD') or '',
        'platform': values.get('LR_ERP_CAPTURE_PLATFORM', 'Tiktok'),
        'platform_id': values.get('LR_ERP_CAPTURE_PLATFORM_ID', '9B583641B0CA11EDAAB1AC1F6B9B9987'),
        'bloc': values.get('LR_ERP_CAPTURE_BLOC', '集团二部'),
        'interval': float(values.get('LR_ERP_CAPTURE_REQUEST_INTERVAL_SECONDS', '16')),
    }


class Reporter:
    def __init__(self, folder):
        self.path = folder / 'progress.log'

    def emit(self, message):
        line = f'[{stamp()}] {message}'
        print(line, flush=True)
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(line + '\n')


class Erp:
    def __init__(self, cfg, client, *, clock=time.monotonic, sleep=time.sleep):
        self.cfg, self.client = cfg, client
        self.clock, self.sleep = clock, sleep
        self.token = None
        self.last_request = None
        self.stats = {'requests': 0, 'http_seconds': 0.0, 'throttle_seconds': 0.0,
                      'rate_limit_hits': 0, 'stages': {}}

    @contextmanager
    def stage(self, name):
        started = self.clock()
        try:
            yield
        finally:
            stages = self.stats['stages']
            stages[name] = stages.get(name, 0.0) + self.clock() - started

    def wait(self, seconds):
        before = self.clock()
        self.sleep(seconds)
        self.stats['throttle_seconds'] += self.clock() - before

    def request(self, method, address, *, retry_delays=(), **kwargs):
        def send():
            if self.last_request is not None:
                remaining = self.cfg['interval'] - (self.clock() - self.last_request)
                if remaining > 0:
                    self.wait(remaining)
            self.last_request = self.clock()
            self.stats['requests'] += 1
            try:
                response = self.client.request(method, address, timeout=120, follow_redirects=False, **kwargs)
                if response.status_code == 429:
                    self.stats['rate_limit_hits'] += 1
                if response.status_code >= 300:
                    response.raise_for_status()
                return response
            finally:
                self.stats['http_seconds'] += self.clock() - self.last_request

        try:
            return retry_erp_read(send, delays=retry_delays, wait=self.wait)
        except httpx.HTTPStatusError as error:
            raise CaptureError(f'HTTP {error.response.status_code}') from None
        except httpx.HTTPError as error:
            raise CaptureError(f'HTTP 请求失败或超时（{type(error).__name__}，地址已省略）') from None

    def post(self, path, body, *, binary=False):
        headers = {'authorization': self.token} if self.token else {}
        delays = BLOCKED_LIST_RETRY_DELAYS if path == BLOCKED else ()
        response = self.request('POST', WEB_BASE + path, retry_delays=delays, json=body, headers=headers)
        if binary and response.content.startswith(b'PK'):
            return response.content
        try:
            payload = response.json()
        except ValueError:
            raise CaptureError('ERP 未返回预期 JSON 或 Excel') from None
        if not isinstance(payload, dict):
            raise CaptureError('ERP 响应结构无效')
        code = payload.get('code')
        if code == 500003:
            self.stats['rate_limit_hits'] += 1
        if type(code) is not int or code != (0 if path == BLOCKED else 200):
            suffix = f'（code={code}）' if type(code) is int else ''
            raise CaptureError('ERP 业务请求失败' + suffix)
        if binary:
            raise CaptureError('ERP 未返回 Excel')
        return payload.get('data')

    def login(self):
        with self.stage('login'):
            data = self.post(LOGIN, {'username': self.cfg['username'], 'password': self.cfg['password']})
            token = (data.get('accessToken') or data.get('token')) if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise CaptureError('ERP 登录未返回令牌')
            self.token = token

    def download(self, address):
        target = urljoin(WEB_BASE, address)
        parsed = urlsplit(target)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise CaptureError('下载地址格式无效')
        # 对象存储无需也不能携带 ERP 令牌；包括同主机不同端口。
        headers = {'authorization': self.token} if self.token and parsed.netloc == urlsplit(WEB_BASE).netloc else {}
        return self.request('GET', target, headers=headers).content


def inspect_xlsx(raw, department):
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Workbook contains no default style.*')
        book = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    try:
        sheet = book.active
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        headers = list(next(rows, ()))
        if '订单编号' not in headers:
            raise CaptureError('Excel 缺少订单编号列')
        order_col = headers.index('订单编号')
        dep_col = headers.index('部门') if '部门' in headers else None
        result = {'rows': 0, 'columns': len(headers), 'order_ids': [], 'invalid_order_id_rows': 0,
                  'department_mismatch_rows': 0, 'department_unknown_rows': 0}
        for row in rows:
            if all(value is None for value in row):
                continue
            result['rows'] += 1
            value = row[order_col] if order_col < len(row) else None
            if isinstance(value, str) and value.strip():
                result['order_ids'].append(value.strip())
            else:
                # 数值订单号可能已丢失精度，原件保留，但不据此请求其他订单。
                result['invalid_order_id_rows'] += 1
            dep = row[dep_col] if dep_col is not None and dep_col < len(row) else None
            if not isinstance(dep, str) or not dep.strip():
                result['department_unknown_rows'] += 1
            elif dep.casefold() != department.casefold():
                result['department_mismatch_rows'] += 1
        result['order_ids'] = list(dict.fromkeys(result['order_ids']))
        return result
    finally:
        book.close()


def wait_task(erp, task_name, emit, max_polls=225):
    with erp.stage('task_wait'):
        for attempt in range(1, max_polls + 1):
            data = erp.post(TASKS, {'pageNum': 1, 'pageSize': 100, 'source': 'orderList'})
            records = data.get('records') if isinstance(data, dict) else data
            if not isinstance(records, list):
                raise CaptureError('任务列表结构无效')
            matches = [r for r in records if isinstance(r, dict) and r.get('source') == 'orderList'
                       and r.get('fileName') == task_name]
            if len(matches) > 1:
                raise CaptureError('本次导出任务存在多条同名记录')
            emit(f'任务查询 {attempt}/{max_polls}：' + ('已找到本次任务' if matches else '尚未找到本次任务'))
            if not matches:
                continue
            task = matches[0]
            status = str(task.get('status') or '').lower()
            if status in ('failure', 'failed', 'interrupted', 'error'):
                raise CaptureError('本次导出任务失败')
            if status in ('success', 'finished', 'complete', 'completed'):
                address = task.get('downloadUrl')
                if not isinstance(address, str) or not address:
                    raise CaptureError('本次导出任务缺少下载地址')
                return address
    raise CaptureError('任务查询次数已用完；不自动重新提交导出')


def safe_error(error):
    return str(error) if isinstance(error, CaptureError) else f'{type(error).__name__}（详情省略，防止泄露凭据或下载地址）'


def collect_group(erp, department, source, folder, args, emit, *, max_polls=225):
    folder.mkdir(parents=True, exist_ok=True)
    started = erp.clock()
    initial_stats = {key: value for key, value in erp.stats.items() if key != 'stages'}
    initial_stages = dict(erp.stats['stages'])
    kind, status = SOURCES[source]
    group = {'department': department, 'source': source, 'started_at': stamp(), 'list_complete': False,
             'candidate_order_ids': [], 'selected_order_ids': [], 'downloaded_order_ids': [],
             'pages': [], 'files': [], 'batches': [], 'errors': [], 'finance_rows': 0}
    ids = group['candidate_order_ids']
    seen = set()

    def persist():
        group['elapsed_seconds'] = erp.clock() - started
        group['timing'] = {key: erp.stats[key] - value for key, value in initial_stats.items()}
        group['timing']['stages'] = {key: value - initial_stages.get(key, 0.0)
                                     for key, value in erp.stats['stages'].items()}
        save_json(folder / 'result.json', group)

    try:
        with erp.stage('candidate_collection'):
            if kind == 'loss':
                raw = erp.post(LOSS, {'platform': erp.cfg['platform'], 'bloc': erp.cfg['bloc'],
                                     'status': status, 'financecheck': '3', 'department': department}, binary=True)
                path = folder / '亏损池.xlsx'
                path.write_bytes(raw)
                group['files'].append({'file': str(path), 'bytes': len(raw)})
                with erp.stage('parse'):
                    info = inspect_xlsx(raw, department)
                group['pool'] = info
                ids.extend(info['order_ids'])
                group['list_complete'] = True
            else:
                page = 1
                while True:
                    data = erp.post(BLOCKED, {'ptid': erp.cfg['platform_id'], 'blocs': erp.cfg['bloc'],
                                            'status': status, 'jzfh': '1', 'departments': department,
                                            'page': page, 'limit': 100})
                    records = data.get('records') if isinstance(data, dict) else None
                    if not isinstance(records, list):
                        raise CaptureError('禁止发货分页结构无效')
                    total = data.get('total')
                    invalid = 0
                    for row in records:
                        value = row.get('orderid') if isinstance(row, dict) else None
                        if isinstance(value, str) and value.strip():
                            value = value.strip()
                            if value not in seen:
                                seen.add(value)
                                ids.append(value)
                        else:
                            invalid += 1
                    group['pages'].append({'page': page, 'rows': len(records),
                                           'total_reported': total if type(total) is int else None,
                                           'invalid_order_id_rows': invalid})
                    emit(f'{department} / {source}：第 {page} 页，累计 {len(ids)} 单')
                    persist()
                    if len(records) < 100 or (type(total) is int and page * 100 >= total):
                        group['list_complete'] = True
                        break
                    page += 1
    except Exception as error:
        group['errors'].append({'stage': 'candidate_collection', 'page': len(group['pages']) + 1,
                                'error': safe_error(error)})
        emit(f'{department} / {source}：名单读取失败，保留已取得的 {len(ids)} 单')
    save_json(folder / '候选名单.json', ids)
    selected = ids[:args.max_orders] if args.max_orders is not None else ids[:]
    group['selected_order_ids'] = selected
    downloaded = set()
    for offset in range(0, len(selected), args.batch_size):
        batch_ids = selected[offset:offset + args.batch_size]
        batch = {'number': len(group['batches']) + 1, 'order_ids': batch_ids, 'status': 'running'}
        group['batches'].append(batch)
        batch_started = erp.clock()
        persist()
        try:
            emit(f'{department} / {source}：第 {batch["number"]} 批，提交 {len(batch_ids)} 单导出')
            # 创建导出只提交一次，响应未知时也不重提。
            with erp.stage('export_submit'):
                data = erp.post(EXPORT, {'orderIdList': batch_ids})
                match = re.search(r'下载任务【([^】]+)】', data) if isinstance(data, str) else None
                if not match:
                    raise CaptureError('导出响应缺少本次任务名；不自动重提')
                batch['task_name'] = match.group(1)
            persist()
            address = wait_task(erp, match.group(1), emit, max_polls)
            with erp.stage('download'):
                raw = erp.download(address)
                path = folder / f'订单明细-{batch["number"]:03d}.xlsx'
                path.write_bytes(raw)
                file_info = {'file': str(path), 'bytes': len(raw)}
                group['files'].append(file_info)
                batch['file'] = file_info
            with erp.stage('parse'):
                info = inspect_xlsx(raw, department)
            batch.update(info)
            actual = set(info['order_ids'])
            downloaded.update(actual)
            batch['missing_order_ids'] = sorted(set(batch_ids) - actual)
            batch['unexpected_order_ids'] = sorted(actual - set(batch_ids))
            batch['status'] = 'downloaded'
            group['finance_rows'] += info['rows']
        except Exception as error:
            batch['status'] = 'failed'
            batch['error'] = safe_error(error)
            emit(f'{department} / {source}：第 {batch["number"]} 批失败：{batch["error"]}')
        finally:
            batch['elapsed_seconds'] = erp.clock() - batch_started
            group['downloaded_order_ids'] = sorted(downloaded)
            persist()
    group.update(candidate_orders=len(ids), requested_orders=len(selected),
                 downloaded_unique_orders=len(downloaded),
                 missing_order_ids=sorted(set(selected) - downloaded),
                 unexpected_order_ids=sorted(downloaded - set(selected)))
    has_issue = bool(group['errors']) or any(
        b['status'] == 'failed' or b.get('missing_order_ids') or b.get('unexpected_order_ids')
        or b.get('invalid_order_id_rows') or b.get('department_mismatch_rows') or b.get('department_unknown_rows')
        for b in group['batches'])
    has_issue = has_issue or any(group.get('pool', {}).get(key) for key in (
        'invalid_order_id_rows', 'department_mismatch_rows', 'department_unknown_rows'))
    group['status'] = 'partial' if has_issue else 'complete'
    persist()
    emit(f'{department} / {source}：候选 {len(ids)}，请求 {len(selected)}，下载唯一订单 {len(downloaded)}，耗时 {group["elapsed_seconds"]:.1f} 秒')
    return group


def build_parser():
    parser = argparse.ArgumentParser(description='按部门下载 ERP 四类原始数据并统计耗时；不算价、不上传',
                                     epilog='现有部门：' + '、'.join(ERP_DEPARTMENTS))
    parser.add_argument('--department', action='append', required=True, help='部门名，可重复指定，串行采集')
    parser.add_argument('--source', choices=list(SOURCES), action='append', help='可重复指定，默认四类')
    parser.add_argument('--max-orders', type=int, help='每部门每类明细最多导出 N 单；默认全量名单与明细')
    parser.add_argument('--batch-size', type=int, default=1000, help='每批订单数，默认 1000')
    parser.add_argument('--request-interval', type=float, help='请求最小间隔秒数，默认读取项目配置（通常 16）')
    parser.add_argument('--output', type=Path, default=ROOT / 'output', help='输出根目录')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.department = list(dict.fromkeys(d.strip() for d in args.department))
    if not all(args.department):
        parser.error('部门名不能为空')
    if not 1 <= args.batch_size <= 1000 or (args.max_orders is not None and args.max_orders < 1):
        parser.error('batch-size 须为 1–1000；max-orders 须为正整数')
    try:
        cfg = load_config()
    except (ValueError, OSError):
        parser.error('本项目 .env 读取失败或请求间隔不是数字')
    if args.request_interval is not None:
        cfg['interval'] = args.request_interval
    if not 0 <= cfg['interval'] < float('inf'):
        parser.error('请求间隔须为有限非负数')
    if not cfg['username'] or not cfg['password']:
        parser.error('请在本项目 .env 配置 LR_ERP_USERNAME / LR_ERP_PASSWORD')
    folder = args.output.resolve() / ('erp-department-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:8])
    folder.mkdir(parents=True, mode=0o700)
    reporter = Reporter(folder)
    reporter.emit(f'本轮输出：{folder}；只下载数据，不算价、不上传、不推送')
    started = time.monotonic()
    report = {'started_at': stamp(), 'departments': args.department,
              'sources': list(dict.fromkeys(args.source or SOURCES)), 'groups': [],
              'platform': cfg['platform'], 'bloc': cfg['bloc'], 'request_interval_seconds': cfg['interval'],
              'max_orders_per_group': args.max_orders, 'batch_size': args.batch_size,
              'errors': [], 'sample_only': args.max_orders is not None}
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        erp = Erp(cfg, client)
        try:
            erp.login()
            for index, department in enumerate(args.department, 1):
                # 目录名去除路径分隔符，不把部门名称作为可执行路径。
                label = re.sub(r'[^\w\-]', '_', department)[:80]
                for source in report['sources']:
                    group = collect_group(erp, department, source, folder / f'{index:02d}-{label}' / source,
                                          args, reporter.emit)
                    report['groups'].append(group)
                    save_json(folder / 'result.json', report)
        except KeyboardInterrupt:
            report['errors'].append('用户中断；当前来源检查点和已下载原件保留，不自动重提任务')
        except Exception as error:
            report['errors'].append(safe_error(error))
        finally:
            report['finished_at'] = stamp()
            report['elapsed_seconds'] = time.monotonic() - started
            report['timing'] = erp.stats
            candidates = {order for g in report['groups'] for order in g['candidate_order_ids']}
            downloaded = {order for g in report['groups'] for order in g['downloaded_order_ids']}
            report['summary'] = {'candidate_unique_order_ids': len(candidates),
                                 'downloaded_unique_order_ids': len(downloaded),
                                 'note': '按文本订单号跨来源去重；动态来源并非冻结快照，不代表可提价资格'}
            report['status'] = 'complete' if not report['errors'] and all(
                g['status'] == 'complete' for g in report['groups']) else 'partial'
            save_json(folder / 'result.json', report)
            reporter.emit(f'状态 {report["status"]}，用时 {report["elapsed_seconds"]:.1f} 秒，请求 {erp.stats["requests"]} 次；汇总：{folder / "result.json"}')
    return 0 if report['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
