"""2026-11-05网页同样本核实的ERP只读采集，尚不是可执行的定价快照。

网页服务与/open服务分别登录；禁止把开放接口令牌套到网页服务。
仅抽取白名单业务字段。金额保留Decimal字符串和原字段名，不猜测币种、
不把订单差额分摊到多SKU、不把抓取时间当业务更新时间。
"""
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.http import TIMEOUT, _private_request
from app.adapters.erp_validation import build_sample_validation


class ErpBusinessFailure(SourceUnavailable):
    """仅保留数字业务码；未有合同的业务码不推断为无权限。"""

    def __init__(self, code):
        super().__init__(f'ERP接口业务失败（code={code}），需核实服务或权限')
        self.code = code


def _error_evidence(error):
    status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
    kind = ('authentication_rejected' if status == 401 else
            'access_denied' if status == 403 else
            'business_failure' if isinstance(error, ErpBusinessFailure) else
            'read_failed')
    return {'error': _failure(error), 'error_kind': kind, 'http_status': status,
            'business_code': error.code if isinstance(error, ErpBusinessFailure) else None}

WEB_BASE = 'http://103.198.125.2:8077'
LOSS_FIELDS = ('orderid', 'platform', 'shopid', 'shopname', 'bloc', 'department',
               'status', 'orderTime', 'grossprofit', 'humancost', 'difference', 'losstime')
ORDER_FIELDS = ('orderid', 'platform', 'shopid', 'shopname', 'status', 'currency',
                'sellNum', 'sku', 'skuid', 'itemid', 'grossProfit', 'revisedGrossProfit',
                'reviseGrossProfitOnce')
DETAIL_FIELDS = ('orderid', 'sku', 'skuid', 'qty', 'currency', 'costPrice', 'originSellPrice',
                 'sellPrice', 'amount', 'exchangeRate', 'storageId', 'storageName', 'weight',
                 'inventory', 'platItemid', 'originSku', 'adjustGrossProfit', 'itempriceinclvat')


def _project(row, fields):
    if not isinstance(row, dict):
        raise SourceUnavailable('ERP记录格式无效')
    return {k: str(row[k]) if isinstance(row.get(k), Decimal) else row.get(k) for k in fields}


def _failure(error):
    # 只有明确HTTP拒绝才标记鉴权/授权问题；超时与业务失败不能推断权限不足。
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
        return 'ERP拒绝认证或授权，请核实该接口和数据范围权限'
    if isinstance(error, SourceUnavailable):
        return str(error)
    return 'ERP读取失败或返回格式无效，需核实服务和响应结构'


def capture_erp_sample(settings, *, limit=5, client=None):
    """每组只取第一页1至10条，明确不宣称全量；失败组隔离并保留结果。

    status=4可配货、3待处理；亏损接口bloc单数，订单接口blocs复数。
    jzfh必须在两个状态中均显式带上。balance=3是界面可选差额过滤，
    此处不擅自用于业务选品。平台ID来自已观察请求，不能替换成平台名称。
    """
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('每组抽样数量必须为1至10')
    if not settings.erp_username or not settings.erp_password.get_secret_value():
        raise SourceUnavailable('请在项目.env配置ERP账号密码，网页接口需独立登录')
    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT, trust_env=False, follow_redirects=False)
    context = _private_request.set(True)
    try:
        def post(path, body=None, token=None, login=False):
            response = client.post(WEB_BASE + path, json=body,
                                   headers={'authorization': token} if token else {},
                                   timeout=TIMEOUT, follow_redirects=False)
            response.raise_for_status()
            payload = response.json(parse_float=Decimal)
            expected = 200 if login else 0
            if isinstance(payload, dict) and type(payload.get('code')) is int and payload['code'] != expected:
                # 仅保留数字业务码；不转发可能含业务信息的msg或完整响应。
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
        groups, details, seen = [], [], set()
        for kind in ('loss', 'blocked'):
            for status in (4, 3):
                path = '/Api/deficit/list' if kind == 'loss' else '/Api/order/list'
                filters = ({'platform': settings.erp_capture_platform, 'bloc': settings.erp_capture_bloc}
                           if kind == 'loss' else {'ptid': settings.erp_capture_platform_id,
                                                  'blocs': settings.erp_capture_bloc, 'jzfh': '1'})
                body = dict(filters, status=status, page=1, limit=limit)
                group = {'kind': kind, 'status': status, 'endpoint': path, 'filters': body,
                         'rows': [], 'total': None, 'error': None, 'sample_only': True}
                groups.append(group)
                try:
                    data = post(path, body, token)
                    rows = data.get('records')
                    if not isinstance(rows, list) or len(rows) > limit:
                        raise SourceUnavailable('ERP分页记录格式或数量无效')
                    group['rows'] = [_project(r, LOSS_FIELDS if kind == 'loss' else ORDER_FIELDS) for r in rows]
                    # total只接受非负整数；其余结构不原样回显，报告标为未知。
                    # 列表仍保留供核对，不因可选计数字段异常阻断独立明细读取。
                    total = data.get('total')
                    group['total'] = total if type(total) is int and total >= 0 else None
                    for row in group['rows']:
                        order = row.get('orderid')
                        if not isinstance(order, str) or not order.strip():
                            details.append({'orderid': None, 'rows': [],
                                            'error': '订单标识缺失或类型无效，未读取明细',
                                            'loss_allocation_confirmed': False})
                            continue
                        if order in seen:
                            continue
                        seen.add(order)
                        detail = {'orderid': order, 'rows': [], 'error': None,
                                  'loss_allocation_confirmed': False}
                        details.append(detail)
                        try:
                            raw = post('/Api/order/findDetail/' + quote(order, safe=''), token=token)
                            lines = raw.get('detaillist')
                            if not isinstance(lines, list):
                                raise SourceUnavailable('ERP订单明细结构无效')
                            detail['rows'] = [_project(r, DETAIL_FIELDS) for r in lines]
                        except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError) as error:
                            detail.update(_error_evidence(error))
                except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError) as error:
                    group.update(_error_evidence(error))
        result = {'source_version': 'erp-web-sample-20261105-v1',
                'queried_at': datetime.now(timezone.utc).isoformat(), 'pricing_ready': False,
                'complete': False, 'sample_only': True, 'groups': groups, 'details': details,
                'pending_confirmations': ['grossprofit与difference的业务亏损口径及金额币种',
                    '多SKU亏损分摊', '业务更新时间', '在线子SKU映射及同店同站主SKU七完整日销量',
                    'itempriceinclvat实测为标识样式，禁止当作含税价格',
                    '当前商品成本不能覆盖历史订单成本']}
        result['validation'] = build_sample_validation(result)
        return result
    except (httpx.HTTPError, ValueError, TypeError):
        raise SourceUnavailable('ERP只读采集连接或响应无效') from None
    finally:
        if owned:
            client.close()
        _private_request.reset(context)
