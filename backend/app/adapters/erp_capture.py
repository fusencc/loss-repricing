"""2026-11-05网页同样本核实的ERP只读采集，尚不是可执行的定价快照。

网页服务与/open服务分别登录；禁止把开放接口令牌套到网页服务。
仅抽取白名单业务字段。金额保留Decimal字符串和原字段名，不猜测币种、
不把订单差额分摊到多SKU、不把抓取时间当业务更新时间。

管理员诊断只读取四组第一页及样本订单明细，不生成定价快照。
完整候选与订单财务由 erp_bulk_capture.py 批量采集。
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote
from time import monotonic, sleep

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.erp_web import WEB_BASE, ErpAccountRateLimited, ErpBusinessFailure, error_evidence, retry_erp_read
from app.adapters.http import TIMEOUT
from app.adapters.transport_privacy import private_request
from app.adapters.erp_validation import build_sample_validation


LOSS_FIELDS = ('orderid', 'platform', 'shopid', 'shopname', 'bloc', 'department',
               'status', 'orderTime', 'grossprofit', 'humancost', 'difference', 'losstime')
ORDER_FIELDS = ('orderid', 'platform', 'shopid', 'shopname', 'status', 'currency',
                'sellNum', 'sku', 'skuid', 'itemid', 'grossProfit', 'revisedGrossProfit',
                'reviseGrossProfitOnce')
# transactionid仅保留来源候选身份，未确认唯一性前不绑定算价明细。
DETAIL_FIELDS = ('orderid', 'transactionid', 'sku', 'skuid', 'qty', 'currency', 'costPrice', 'originSellPrice',
                 'sellPrice', 'amount', 'exchangeRate', 'storageId', 'storageName', 'weight',
                 'inventory', 'platItemid', 'originSku', 'adjustGrossProfit', 'itempriceinclvat')


def _project(row, fields):
    if not isinstance(row, dict):
        raise SourceUnavailable('ERP记录格式无效')
    return {k: str(row[k]) if isinstance(row.get(k), Decimal) else row.get(k) for k in fields}


@contextmanager
def _erp_reader(settings, client=None):
    """沿用已核实的网页认证和请求合同；不调用任何调价接口。"""
    if not settings.erp_username or not settings.erp_password.get_secret_value():
        raise SourceUnavailable('请在项目.env配置ERP账号密码，网页接口需独立登录')
    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT, trust_env=False, follow_redirects=False)
    context = private_request.set(True)
    last_request_at = None
    # 自带客户端统一节流；注入的客户端由调用者负责限速（例如受控测试/诊断钩子）。
    interval = settings.erp_capture_request_interval_seconds if owned else 0
    try:
        def post(path, body=None, token=None, login=False):
            nonlocal last_request_at
            if last_request_at is not None:
                wait = interval - (monotonic() - last_request_at)
                if wait > 0:
                    sleep(wait)
            last_request_at = monotonic()
            response = client.post(WEB_BASE + path, json=body,
                                   headers={'authorization': token} if token else {},
                                   timeout=TIMEOUT, follow_redirects=False)
            response.raise_for_status()
            payload = response.json(parse_float=Decimal)
            expected = 200 if login else 0
            if isinstance(payload, dict) and type(payload.get('code')) is int and payload['code'] != expected:
                if payload['code'] == 500003:
                    raise ErpAccountRateLimited()
                raise ErpBusinessFailure(payload['code'])
            if (not isinstance(payload, dict) or type(payload.get('code')) is not int
                    or payload['code'] != expected or (login and payload.get('success') is not True)):
                raise SourceUnavailable('ERP接口未返回业务成功，请核实服务与权限')
            if not isinstance(payload.get('data'), dict):
                raise SourceUnavailable('ERP接口返回结构无效')
            return payload['data']

        user = post('/newIp/system/login', {'username': settings.erp_username,
                    'password': settings.erp_password.get_secret_value()}, login=True)
        token = user.get('accessToken') or user.get('token')
        if not isinstance(token, str) or not token.strip():
            raise SourceUnavailable('ERP网页登录未返回令牌')
        yield lambda path, body=None: retry_erp_read(lambda: post(path, body, token))
    except (httpx.HTTPError, ValueError, TypeError):
        raise SourceUnavailable('ERP只读采集连接或响应无效') from None
    finally:
        if owned:
            client.close()
        private_request.reset(context)


def _read_sample_group(post, group, limit):
    page = {'page': 1, 'queried_at': datetime.now(timezone.utc).isoformat(),
            'filters': group['filters'], 'row_count': 0, 'total': None, 'error': None}
    group['pages'].append(page)
    try:
        data = post(group['endpoint'], group['filters'])
        rows = data.get('records')
        if not isinstance(rows, list) or len(rows) > limit:
            raise SourceUnavailable('ERP分页记录格式或数量无效')
        fields = LOSS_FIELDS if group['kind'] == 'loss' else ORDER_FIELDS
        projected = [_project(row, fields) for row in rows]
        total = data.get('total')
        total = total if type(total) is int and total >= 0 else None
        page.update(row_count=len(projected), total=total, excluded_platform_rows=0)
        group.update(total=total, latest_total=total, rows=projected)
    except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError) as error:
        evidence = error_evidence(error)
        page.update(evidence)
        group.update(evidence, failed_pages=[1], stop_reason='read_failed')
        if isinstance(error, ErpAccountRateLimited):
            raise


def _read_sample_detail(post, detail):
    try:
        raw = post('/Api/order/findDetail/' + quote(detail['orderid'], safe=''))
        lines = raw.get('detaillist')
        if not isinstance(lines, list):
            raise SourceUnavailable('ERP订单明细结构无效')
        detail['rows'] = [_project(row, DETAIL_FIELDS) for row in lines]
    except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError) as error:
        detail.update(error_evidence(error))
        if isinstance(error, ErpAccountRateLimited):
            raise


def _summarize_sample_group(group, limit):
    rows = group['rows']
    order_ids = [row['orderid'] for row in rows
                 if isinstance(row.get('orderid'), str) and row['orderid'].strip()]
    unique_orders = len(set(order_ids))
    group.update(fetched_rows=len(rows), unique_orders=unique_orders,
                 duplicate_rows=len(order_ids) - unique_orders)
    if group['error']:
        return
    total = group['total']
    if not rows:
        complete = total == 0
        reason = 'empty_page' if complete else 'total_unknown' if total is None else 'count_inconsistent'
    elif total is not None and len(rows) >= total:
        complete = total == len(rows) == unique_orders
        reason = 'total_reached' if complete else 'count_inconsistent'
    elif total is None:
        complete, reason = False, 'total_unknown'
    else:
        complete, reason = False, 'page_limit'
    group.update(list_complete=complete, stop_reason=reason)


def capture_erp_sample(settings, *, limit=5, client=None):
    """每组只读第一页；同轮重复订单只读一次明细，金额保留来源十进制字符串。"""
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('每组抽样数量必须为1至10')
    started_at = datetime.now(timezone.utc).isoformat()
    groups, details, detail_by_order = [], [], {}
    rate_limited = False
    with _erp_reader(settings, client) as post:
        try:
            for kind in ('loss', 'blocked'):
                for status in (4, 3):
                    path = '/Api/deficit/list' if kind == 'loss' else '/Api/order/list'
                    # status=4可配货、3待处理；bloc/blocs及jzfh沿用已核实的网页筛选。
                    filters = ({'platform': settings.erp_capture_platform, 'bloc': settings.erp_capture_bloc}
                               if kind == 'loss' else {'ptid': settings.erp_capture_platform_id,
                                                      'blocs': settings.erp_capture_bloc, 'jzfh': '1'})
                    group = {'kind': kind, 'status': status, 'endpoint': path,
                             'filters': dict(filters, status=status, page=1, limit=limit),
                             'rows': [], 'total': None, 'error': None, 'sample_only': True,
                             'pages': [], 'failed_pages': [], 'list_complete': False, 'excluded_platform_rows': 0}
                    groups.append(group)
                    _read_sample_group(post, group, limit)
                    for row in group['rows']:
                        order = row.get('orderid')
                        source = {'kind': kind, 'status': status}
                        if not isinstance(order, str) or not order.strip():
                            details.append({'orderid': None, 'rows': [], 'sources': [source],
                                            'error': '订单标识缺失或类型无效，未读取明细',
                                            'loss_allocation_confirmed': False})
                            continue
                        if order in detail_by_order:
                            sources = detail_by_order[order]['sources']
                            if source not in sources:
                                sources.append(source)
                            continue
                        detail = {'orderid': order, 'rows': [], 'sources': [source], 'error': None,
                                  'loss_allocation_confirmed': False}
                        details.append(detail)
                        detail_by_order[order] = detail
                        _read_sample_detail(post, detail)
                    _summarize_sample_group(group, limit)
        except ErpAccountRateLimited:
            rate_limited = True
    for group in groups:
        group.setdefault('fetched_rows', len(group['rows']))
        group.setdefault('unique_orders', len({r.get('orderid') for r in group['rows'] if isinstance(r.get('orderid'), str)}))
        group.setdefault('duplicate_rows', len(group['rows']) - group['unique_orders'])
        if rate_limited and 'stop_reason' not in group:
            group['stop_reason'] = 'account_rate_limited'
    result = {'source_version': 'erp-web-sample-20261105-v1',
              'queried_at': started_at, 'finished_at': datetime.now(timezone.utc).isoformat(),
              'source_kind': 'erp_web_backend', 'finance_enabled': False,
              'request_interval_seconds': settings.erp_capture_request_interval_seconds,
              'stopped_by_rate_limit': rate_limited,
              'not_read_groups': [{'kind': k, 'status': st} for k in ('loss','blocked') for st in (4,3)
                                  if not any(g['kind']==k and g['status']==st for g in groups)],
              'page_size': limit, 'max_pages': 1, 'pricing_ready': False,
              'complete': False, 'sample_only': True, 'groups': groups, 'details': details,
              'permission_scope_verified': False,
              'pending_confirmations': ['grossprofit与difference的业务亏损口径及金额币种',
                  '多SKU亏损分摊', '业务更新时间', '在线子SKU映射及同店同站主SKU七完整日销量',
                  'itempriceinclvat实测为标识样式，禁止当作含税价格',
                  '当前商品成本不能覆盖历史订单成本']}
    result['capture_complete'] = False
    result['summary'] = {
        'list_rows': sum(g['fetched_rows'] for g in groups), 'unique_orders': len({r['orderid'] for g in groups for r in g['rows']
                              if isinstance(r.get('orderid'), str) and r['orderid'].strip()}),
        'list_groups_complete': sum(g['list_complete'] for g in groups),
        'failed_pages': sum(len(g['failed_pages']) for g in groups),
        'detail_reads_succeeded': sum(d['orderid'] is not None and not d['error'] for d in details),
        'detail_reads_failed': sum(d['orderid'] is not None and bool(d['error']) for d in details),
        'missing_order_ids': sum(d['orderid'] is None for d in details),
        'detail_rows': sum(len(d['rows']) for d in details)}
    result['summary']['detail_orders_not_attempted'] = result['summary']['unique_orders'] - len(detail_by_order)
    result['validation'] = build_sample_validation(result)
    return result
