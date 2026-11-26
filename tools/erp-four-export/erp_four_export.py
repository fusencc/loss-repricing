"""ERP 四类筛选数据下载：亏损/禁止发货 × 可配货/待处理。

全程低频访问：任意两次请求至少间隔 interval（默认 20 秒，比日常采集的 16 秒更宽），
任务轮询间隔逐次拉长到 90 秒封顶；命中 500003/429 落冷却并停止，不硬撞。

用法：
  python erp_four_export.py probe          # 只探测数量，不下载
  python erp_four_export.py run            # 下载四类（可分 --only 指定）
  python erp_four_export.py run --only 1,3 # 只跑 1 亏损+可配货、3 禁止发货+可配货
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values

BASE = 'http://103.198.125.2:8077'
LOGIN = '/newIp/system/login'
LOSS_EXPORT = '/Api/deficit/dowDeficitOrder'
BLOCKED_LIST = '/Api/order/list'
ORDER_EXPORT = '/newIp/report/orderDownload/asyncOrderListDownload'
TASKS = '/newIp/report/download/list'

CASES = {
    '1': {'key': 'loss_allocatable', 'kind': 'loss', 'status': 4, 'label': '亏损-可配货'},
    '2': {'key': 'loss_pending', 'kind': 'loss', 'status': 3, 'label': '亏损-待处理'},
    '3': {'key': 'blocked_allocatable', 'kind': 'blocked', 'status': 4, 'label': '禁止发货-可配货'},
    '4': {'key': 'blocked_pending', 'kind': 'blocked', 'status': 3, 'label': '禁止发货-待处理'},
}
STATUS_LABEL = {4: '可配货', 3: '待处理'}

HERE = Path(__file__).resolve().parent
STATE = HERE / 'state'
DEFAULT_OUT = HERE / 'downloads'


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


class Stopped(Exception):
    """命中限流或达到请求上限，本轮停止。"""


class Gate:
    """跨进程节流 + 限流冷却 + 单实例锁；状态落在 state/ 下。"""

    def __init__(self, interval=20.0, cooldown=600.0, cooldown_cap=1800.0, max_hits=2, stale=900.0):
        self.root = STATE
        self.root.mkdir(parents=True, exist_ok=True)
        self.interval = float(interval)
        self.cooldown = float(cooldown)
        self.cooldown_cap = float(cooldown_cap)
        self.max_hits = int(max_hits)
        self.stale = float(stale)
        self.turn_path = self.root / 'last_request.json'
        self.turn_lock = self.root / 'last_request.lock'
        self.cooldown_path = self.root / 'cooldown.json'
        self.instance_path = self.root / 'instance.lock'
        self.requests = 0
        self.hits = 0

    # ---- 文件状态 ----
    @staticmethod
    def _read(path):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(payload), encoding='utf-8')
        os.replace(tmp, path)

    class _Exclusive:
        def __init__(self, path, timeout=20.0):
            self.path = Path(path)
            self.timeout = timeout
            self.fd = -1
            self.owned = False

        def __enter__(self):
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(self.fd, str(os.getpid()).encode())
                    self.owned = True
                    return self
                except FileExistsError:
                    if time.monotonic() > deadline:
                        try:
                            os.unlink(str(self.path))
                        except OSError:
                            pass
                        deadline = time.monotonic() + self.timeout
                        continue
                    time.sleep(0.02)

        def __exit__(self, *exc):
            if self.owned:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                try:
                    os.unlink(str(self.path))
                except OSError:
                    pass
            return False

    # ---- 单实例 ----
    def acquire_instance(self, wait=0.0):
        deadline = time.monotonic() + max(0.0, float(wait))
        while True:
            try:
                fd = os.open(str(self.instance_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps({'pid': os.getpid(), 'started': now_iso()}).encode())
                os.close(fd)
                return True
            except FileExistsError:
                info = self._read(self.instance_path)
                age = time.time() - self.instance_path.stat().st_mtime
                pid = info.get('pid')
                alive = pid == os.getpid() or (isinstance(pid, int) and _pid_alive(pid))
                if age > self.stale or not alive:
                    try:
                        os.unlink(str(self.instance_path))
                    except OSError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    raise Stopped(f'已有另一个ERP下载进程在运行（pid={pid}），本次不启动，避免同时打接口')
                time.sleep(3)

    def release_instance(self):
        try:
            os.unlink(str(self.instance_path))
        except OSError:
            pass

    # ---- 冷却与节流 ----
    def cooldown_remaining(self):
        return max(0.0, float(self._read(self.cooldown_path).get('until') or 0) - time.time())

    def note_rate_limit(self):
        self.hits += 1
        stamp = time.time()
        self._write(self.cooldown_path, {'until': stamp + self.cooldown, 'hits': self.hits, 'noted_at': stamp})
        return self.hits

    def note_success(self):
        self.hits = 0

    def wait(self, spacing=None, limit=None):
        remaining = self.cooldown_remaining()
        if remaining > 0:
            if remaining > self.cooldown_cap:
                raise Stopped(f'ERP限流冷却剩余 {int(remaining)} 秒，超过可等待上限，本轮停止')
            print(f'  冷却中，等待 {int(remaining)} 秒…', flush=True)
            time.sleep(remaining)
        target = max(self.interval, float(spacing or 0.0))
        with Gate._Exclusive(self.turn_lock):
            last = float(self._read(self.turn_path).get('at') or 0)
            gap = time.time() - last
            if gap < target:
                wait_for = target - gap
                time.sleep(wait_for)
            self._write(self.turn_path, {'at': time.time()})
        self.requests += 1
        if limit is not None and self.requests > limit:
            raise Stopped(f'本轮请求数达到上限 {limit}，停止（防止对ERP造成压力）')


def _pid_alive(pid):
    try:
        if os.name == 'nt':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


class Erp:
    def __init__(self, cfg, gate):
        self.cfg = cfg
        self.gate = gate
        self.client = httpx.Client(timeout=httpx.Timeout(120.0), trust_env=False, follow_redirects=False)
        self.token = None
        self.journal_path = STATE / 'journal.jsonl'

    def close(self):
        self.client.close()

    def _log(self, method, path, status, elapsed, note=''):
        try:
            with self.journal_path.open('a', encoding='utf-8') as fh:
                fh.write(json.dumps({'at': now_iso(), 'method': method, 'path': path,
                                     'status': status, 'elapsed': round(elapsed, 2), 'note': note},
                                    ensure_ascii=False) + '\n')
        except OSError:
            pass

    def login(self):
        self.gate.wait(limit=self.cfg['max_requests'])
        t0 = time.monotonic()
        r = self.client.post(BASE + LOGIN, json={'username': self.cfg['username'],
                                                 'password': self.cfg['password']})
        self._log('POST', LOGIN, r.status_code, time.monotonic() - t0)
        if r.status_code == 429:
            self.gate.note_rate_limit()
            raise Stopped('登录被网关限流(429)')
        r.raise_for_status()
        body = r.json()
        code = body.get('code') if isinstance(body, dict) else None
        if code == 500003:
            self.gate.note_rate_limit()
            raise Stopped('登录返回500003（账号操作频繁）')
        data = body.get('data') if isinstance(body, dict) else None
        token = (data or {}).get('accessToken') or (data or {}).get('token') if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise Stopped(f'登录未返回令牌（code={code}），请核对账号或权限')
        self.token = token
        print(f'登录成功（账号 {self.cfg["username"]}）', flush=True)

    def post(self, path, body, *, spacing=None, binary=False, expect=200, retries=1):
        """所有请求过闸门；429/500003 落冷却并抛出；5xx 有限重试。"""
        relogin = True
        for attempt in range(retries + 1):
            if self.token is None:
                self.login()
            self.gate.wait(spacing=spacing if attempt == 0 else max(60.0, float(spacing or 0)), limit=self.cfg['max_requests'])
            t0 = time.monotonic()
            try:
                r = self.client.post(BASE + path, json=body, headers={'authorization': self.token})
            except httpx.TimeoutException as exc:
                self._log('POST', path, 'timeout', time.monotonic() - t0, type(exc).__name__)
                if attempt >= retries:
                    raise Stopped(f'请求超时：{path}')
                continue
            elapsed = time.monotonic() - t0
            self._log('POST', path, r.status_code, elapsed)
            if r.status_code == 429:
                self.gate.note_rate_limit()
                raise Stopped(f'网关限流(429)：{path}')
            if r.status_code == 401 and relogin:
                relogin = False
                self.token = None
                continue
            if 500 <= r.status_code <= 599 and attempt < retries:
                continue
            r.raise_for_status()
            if binary:
                if r.content[:2] == b'PK':
                    self.gate.note_success()
                    return r.content
                raise Stopped(f'期望XLSX，实际返回非文件内容（{path}）')
            payload = r.json()
            if not isinstance(payload, dict) or type(payload.get('code')) is not int:
                raise Stopped(f'返回结构无效：{path}')
            if payload['code'] == 500003:
                self.gate.note_rate_limit()
                raise Stopped(f'返回500003（账号操作频繁）：{path}')
            if payload['code'] != expect:
                raise Stopped(f'业务失败 code={payload["code"]}：{path}')
            self.gate.note_success()
            return payload.get('data')

    def download(self, url, dest: Path):
        text = str(url)
        target = text if text.startswith(('http://', 'https://')) else BASE + text
        parsed = httpx.URL(target)
        # 下载服务在另一端口且会拒绝带 authorization 头（返回400），只在同一站点时才带令牌。
        headers = {'authorization': self.token} if parsed.netloc == httpx.URL(BASE).netloc else {}
        last_error = None
        for attempt in range(3):
            self.gate.wait(spacing=None if attempt == 0 else 40.0, limit=self.cfg['max_requests'])
            t0 = time.monotonic()
            try:
                r = self.client.get(parsed, headers=headers)
            except httpx.HTTPError as exc:
                self._log('GET', parsed.path, 'error', time.monotonic() - t0, type(exc).__name__)
                last_error = exc
                continue
            self._log('GET', parsed.path, r.status_code, time.monotonic() - t0)
            if r.status_code == 429:
                self.gate.note_rate_limit()
                raise Stopped('下载被限流(429)')
            if 500 <= r.status_code <= 599 and attempt < 2:
                last_error = httpx.HTTPStatusError(f'{r.status_code}', request=r.request, response=r)
                continue
            r.raise_for_status()
            if r.content[:2] != b'PK':
                raise Stopped('下载内容不是XLSX文件')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            self.gate.note_success()
            return len(r.content)
        raise Stopped(f'下载失败：{last_error}')


def loss_filters(cfg, status):
    return {'platform': cfg['platform'], 'bloc': cfg['bloc'], 'status': status,
            'financecheck': cfg['financecheck']}


def blocked_filters(cfg, status):
    return {'ptid': cfg['platform_id'], 'blocs': cfg['bloc'], 'status': status, 'jzfh': '1'}


def read_order_ids(xlsx_path):
    """读XLSX取订单编号列；非文本单元格可能丢精度，单独计数。"""
    from openpyxl import load_workbook
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        book = load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        sheet = book.active
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        headers = [str(h or '').strip() for h in next(rows, ())]
        if '订单编号' not in headers:
            raise Stopped(f'{xlsx_path.name} 缺少“订单编号”列，无法继续导出明细')
        idx = headers.index('订单编号')
        ids, numeric = [], 0
        for values in rows:
            value = values[idx] if idx < len(values) else None
            if value is None or value == '':
                continue
            if isinstance(value, str):
                text = value.strip()
                if text:
                    ids.append(text)
            else:
                numeric += 1
                text = str(value)
                if text.endswith('.0'):
                    text = text[:-2]
                ids.append(text)
        return list(dict.fromkeys(ids)), numeric, len(headers)
    finally:
        book.close()


def submit_export(erp, ids, tag):
    data = erp.post(ORDER_EXPORT, {'orderIdList': ids})
    name = None
    if isinstance(data, str):
        match = re.search(r'下载任务【([^】]+)】', data)
        name = match.group(1) if match else None
    if not name:
        raise Stopped(f'{tag} 导出接口未返回任务名：{str(data)[:60]}')
    return name


def poll_task(erp, task_name, cfg):
    """按任务名精确匹配下载列表，退避轮询；返回下载地址。"""
    spacing = max(float(cfg['interval']), 20.0)
    deadline = time.monotonic() + float(cfg['poll_budget_seconds'])
    not_found = 0
    for attempt in range(1, int(cfg['poll_max_attempts']) + 1):
        if time.monotonic() > deadline:
            raise Stopped(f'等待任务 {task_name} 超时（{int(cfg["poll_budget_seconds"])} 秒预算）')
        data = erp.post(TASKS, {'pageNum': 1, 'pageSize': 100, 'source': 'orderList'}, spacing=spacing)
        records = data.get('records') if isinstance(data, dict) else data
        if not isinstance(records, list):
            raise Stopped('下载列表结构无效')
        matched = [t for t in records if isinstance(t, dict)
                   and t.get('source') == 'orderList' and t.get('fileName') == task_name]
        if len(matched) > 1:
            raise Stopped(f'任务名 {task_name} 存在多条匹配，停止以免拿错文件')
        task = matched[0] if matched else None
        if task is None:
            not_found += 1
            print(f'  第{attempt}次查询：任务尚未出现在下载列表首页（连续{not_found}次）', flush=True)
            if not_found >= int(cfg['poll_notfound_limit']):
                raise Stopped(f'任务 {task_name} 连续 {not_found} 次不在下载列表首页，停止（不盲查）')
        else:
            not_found = 0
            status = str(task.get('status') or '').lower()
            print(f'  第{attempt}次查询：状态 {status or "未知"}', flush=True)
            if status in ('failure', 'failed', 'interrupted', 'error'):
                raise Stopped(f'ERP导出任务失败：{task_name}')
            if status in ('success', 'finished', 'complete', 'completed'):
                url = task.get('downloadUrl')
                if not url:
                    raise Stopped(f'任务成功但无下载地址：{task_name}')
                return url
        spacing = min(max(spacing * 1.5, spacing + 10), float(cfg['poll_max_spacing']))
    raise Stopped(f'任务 {task_name} 轮询次数用尽')


def collect_blocked_ids(erp, cfg, status):
    ids, page, total = [], 1, None
    seen = set()
    stalled = 0
    while page <= int(cfg['max_pages']):
        body = dict(blocked_filters(cfg, status), page=page, limit=int(cfg['page_size']))
        data = erp.post(BLOCKED_LIST, body, expect=0, retries=2)
        records = data.get('records') if isinstance(data, dict) else None
        total = data.get('total') if isinstance(data, dict) else None
        if not isinstance(records, list) or type(total) is not int:
            raise Stopped('禁止发货分页结构无效')
        added = 0
        for row in records:
            order = row.get('orderid') if isinstance(row, dict) else None
            if isinstance(order, str) and order.strip() and order not in seen:
                seen.add(order)
                ids.append(order)
                added += 1
        print(f'  禁止发货 第{page}页：返回{len(records)}行/新增{added}，累计{len(ids)}/{total}', flush=True)
        if len(records) < int(cfg['page_size']) or page * int(cfg['page_size']) >= total:
            break
        stalled = stalled + 1 if added == 0 else 0
        if stalled >= 2:
            raise Stopped('连续两页无新增，源端可能未按页推进，停止分页')
        page += 1
    return ids, total


def run_case(erp, cfg, case, out_dir, probe_only=False):
    result = {'case': case['label'], 'key': case['key'], 'status': case['status'],
              'candidates': None, 'files': [], 'batches': [], 'error': None}
    status = case['status']
    if case['kind'] == 'loss':
        print(f'[{case["label"]}] 请求亏损池导出（1次请求）…', flush=True)
        raw = erp.post(LOSS_EXPORT, loss_filters(cfg, status), binary=True, retries=2)
        if probe_only:
            probe_path = out_dir / f'{case["key"]}-亏损池.xlsx'
            probe_path.write_bytes(raw)
            ids, numeric, columns = read_order_ids(probe_path)
            result.update(candidates=len(ids), numeric_order_ids=numeric, pool_columns=columns,
                          files=[{'file': str(probe_path), 'bytes': len(raw)}])
            print(f'  亏损池 {len(ids)} 单 / {columns} 列', flush=True)
            return result
        pool = out_dir / f'{case["key"]}-亏损池.xlsx'
        pool.write_bytes(raw)
        ids, numeric, columns = read_order_ids(pool)
        result['files'].append({'file': str(pool), 'bytes': len(raw)})
        result['candidates'] = len(ids)
        result['numeric_order_ids'] = numeric
        result['pool_columns'] = columns
        print(f'  亏损池 {len(ids)} 单（{columns} 列，非文本订单号 {numeric} 个）', flush=True)
    else:
        if probe_only:
            data = erp.post(BLOCKED_LIST, dict(blocked_filters(cfg, status), page=1, limit=int(cfg['page_size'])),
                            expect=0, retries=2)
            records = data.get('records') if isinstance(data, dict) else None
            total = data.get('total') if isinstance(data, dict) else None
            sample = list(records[0].keys()) if records and isinstance(records[0], dict) else []
            result.update(candidates=total, total_reported=total, sample_fields=sample)
            print(f'  禁止发货 {STATUS_LABEL[status]}：服务端 total={total}，样例字段 {sample}', flush=True)
            return result
        print(f'[{case["label"]}] 分页取禁止发货名单…', flush=True)
        ids, total = collect_blocked_ids(erp, cfg, status)
        result['candidates'] = len(ids)
        result['total_reported'] = total
        if probe_only:
            return result
        if not ids:
            result['error'] = '名单为空'
            return result
        list_path = out_dir / f'{case["key"]}-名单.json'
        list_path.write_text(json.dumps(ids, ensure_ascii=False), encoding='utf-8')
        result['files'].append({'file': str(list_path), 'bytes': list_path.stat().st_size})

    if probe_only:
        return result
    if not ids:
        result['error'] = '无候选订单，跳过明细导出'
        return result

    if cfg.get('max_orders'):
        ids = ids[:int(cfg['max_orders'])]
        print(f'  按 --max-orders 限制为 {len(ids)} 单', flush=True)

    batch_size = int(cfg['batch_size'])
    batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
    for number, batch in enumerate(batches, start=1):
        info = {'number': number, 'orders': len(batch), 'task': None, 'file': None, 'error': None}
        result['batches'].append(info)
        print(f'  第{number}/{len(batches)}批：提交 {len(batch)} 单导出任务…', flush=True)
        name = submit_export(erp, batch, case['label'])
        info['task'] = name
        url = poll_task(erp, name, cfg)
        dest = out_dir / f'{case["key"]}-订单明细-{number:03d}.xlsx'
        size = erp.download(url, dest)
        info['file'] = str(dest)
        info['bytes'] = size
        print(f'  已下载：{dest.name}（{size/1024:.0f} KB）', flush=True)
    return result


def load_config(args):
    cfg = {
        'base': BASE,
        'username': '',
        'password': '',
        'platform': 'Tiktok',
        'platform_id': '9B583641B0CA11EDAAB1AC1F6B9B9987',
        'bloc': '集团二部',
        'financecheck': '3',
        'interval': 20.0,
        'cooldown_seconds': 600.0,
        'cooldown_cap_seconds': 1800.0,
        'max_rate_limit_hits': 2,
        'page_size': 100,
        'max_pages': 500,
        'batch_size': 1000,
        'poll_max_attempts': 40,
        'poll_budget_seconds': 3600.0,
        'poll_max_spacing': 90.0,
        'poll_notfound_limit': 10,
        'max_requests': 500,
        'max_orders': 0,
    }
    path = HERE / 'config.json'
    if path.exists():
        try:
            cfg.update({k: v for k, v in json.loads(path.read_text(encoding='utf-8')).items()
                        if v is not None and k not in ('username', 'password')})
        except ValueError:
            print('config.json 不是有效JSON，改用内置默认值', flush=True)
    # 只读取本项目配置，不把账号复制到工具目录，也不搜索其他项目。
    values = {**dotenv_values(HERE.parents[1] / '.env'), **os.environ}
    cfg['username'] = values.get('LR_ERP_USERNAME') or ''
    cfg['password'] = values.get('LR_ERP_PASSWORD') or ''
    for key, env in (('platform', 'LR_ERP_CAPTURE_PLATFORM'),
                     ('platform_id', 'LR_ERP_CAPTURE_PLATFORM_ID'), ('bloc', 'LR_ERP_CAPTURE_BLOC')):
        if values.get(env):
            cfg[key] = values[env]
    for name in ('interval', 'bloc', 'page_size', 'batch_size', 'max_requests', 'max_orders', 'poll_max_spacing'):
        value = getattr(args, name, None)
        if value is not None:
            cfg[name] = value
    return cfg


def main():
    parser = argparse.ArgumentParser(description='ERP 四类筛选数据下载（低频）')
    parser.add_argument('mode', choices=['probe', 'run'], help='probe=只探测数量；run=下载')
    parser.add_argument('--only', default='1,2,3,4', help='要跑的序号，逗号分隔，如 1,3')
    parser.add_argument('--interval', type=float, default=None, help='两次请求最小间隔秒数（默认20）')
    parser.add_argument('--page-size', type=int, default=None, help='禁止发货分页大小（默认100）')
    parser.add_argument('--batch-size', type=int, default=None, help='每批导出订单数（默认1000）')
    parser.add_argument('--max-orders', type=int, default=None, help='每类最多导出多少单（0=不限）')
    parser.add_argument('--max-requests', type=int, default=None, help='本轮请求硬上限')
    parser.add_argument('--poll-max-spacing', type=float, default=None)
    parser.add_argument('--bloc', default=None)
    parser.add_argument('--out', default=None, help='输出目录，默认 downloads/<时间戳>')
    parser.add_argument('--lock-wait', type=float, default=0.0, help='单实例锁等待秒数')
    args = parser.parse_args()

    cfg = load_config(args)
    if not cfg['username'] or not cfg['password']:
        parser.error('请在本项目根目录 .env 配置 LR_ERP_USERNAME / LR_ERP_PASSWORD')
    selected = [CASES[k] for k in (s.strip() for s in args.only.split(',')) if k in CASES]
    if not selected:
        parser.error('--only 没有有效序号')

    out_dir = Path(args.out) if args.out else DEFAULT_OUT / datetime.now().strftime('%Y%m%d-%H%M')
    out_dir.mkdir(parents=True, exist_ok=True)

    gate = Gate(interval=cfg['interval'], cooldown=cfg['cooldown_seconds'],
                cooldown_cap=cfg['cooldown_cap_seconds'], max_hits=cfg['max_rate_limit_hits'])
    try:
        gate.acquire_instance(wait=args.lock_wait)
    except Stopped as exc:
        print(f'未启动：{exc}', flush=True)
        return 3

    erp = None
    started = time.monotonic()
    results = []
    print(f'模式={args.mode} 间隔={cfg["interval"]}秒 分页={cfg["page_size"]} 批次={cfg["batch_size"]} 输出={out_dir}', flush=True)
    try:
        erp = Erp(cfg, gate)
        for case in selected:
            try:
                results.append(run_case(erp, cfg, case, out_dir, probe_only=args.mode == 'probe'))
            except Stopped as exc:
                print(f'[{case["label"]}] 停止：{exc}', flush=True)
                results.append({'case': case['label'], 'key': case['key'], 'status': case['status'],
                                'error': str(exc), 'files': [], 'batches': []})
                if gate.cooldown_remaining() > 0:
                    print('命中限流，本轮剩余类别不再执行。', flush=True)
                    break
    finally:
        if erp:
            erp.close()
        gate.release_instance()

    report = {'mode': args.mode, 'started_at': now_iso(), 'elapsed_seconds': round(time.monotonic() - started, 1),
              'requests': gate.requests, 'rate_limit_hits': gate.hits,
              'cooldown_remaining': round(gate.cooldown_remaining(), 1), 'results': results}
    (out_dir / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n本轮请求 {gate.requests} 次，限流命中 {gate.hits} 次，用时 {report["elapsed_seconds"]} 秒', flush=True)
    for item in results:
        state = '失败：' + item['error'] if item.get('error') else f"候选 {item['candidates']} 单 / {len(item['files'])} 个文件"
        print(f'  {item["case"]}：{state}', flush=True)
    print(f'结果清单：{out_dir / "result.json"}', flush=True)
    return 0 if all(not item.get('error') for item in results) else 1


if __name__ == '__main__':
    sys.exit(main())
