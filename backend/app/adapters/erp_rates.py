"""公司 ERP 汇率只读查询；不构造已确认的定价快照。

依据：用户提供的内部开放接口“用户登录/查询所有汇率”，2026-11-05只读实测。
数据为每币种一条系统当前配置值，不是商品/订单粒度。rate 无生效日期，
now 仅接口响应毫秒时间戳，不能替代 fx_date。实测 USD=6.6269、CNY=1，
推断 rate 为 CNY/外币单位，尚待业务确认；倒数只作为参考展示，不自动用于调价。
"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.http import _private_request, TIMEOUT
from app.config import Settings
from app.domain.money import PRICE_CONTEXT

VERSION = 'erp-rates-readonly-v1'
PATH = '/system/innerOpen/rate/list'
ALLOWED_BASES = {'http://103.198.125.2:16777/open', 'http://103.198.125.5:15672/open'}


def _success(response):
    response.raise_for_status()
    payload = response.json(parse_float=Decimal)
    if not isinstance(payload, dict) or payload.get('success') is not True or payload.get('code') != 200:
        raise SourceUnavailable('ERP汇率接口未返回业务成功，请检查服务和账号权限')
    return payload


def _rows(data):
    currencies = [r.get('currency') if isinstance(r, dict) else None for r in data]
    counts = Counter(c for c in currencies if isinstance(c, str))
    result = []
    for row in data:
        currency = row.get('currency') if isinstance(row, dict) else None
        raw = row.get('rate') if isinstance(row, dict) else None
        error = None
        rate = inverse = None
        # 非标准代码保留供排查，不猜测 baht/US/CDN 等别名，不影响独立的有效币种。
        if not isinstance(currency, str) or re.fullmatch(r'[A-Z]{3}', currency) is None:
            error = '币种代码非标准，未转换'
        elif counts[currency] > 1:
            error = '同币种重复，未选择或覆盖汇率'
        else:
            try:
                if isinstance(raw, bool) or not isinstance(raw, (str, int, Decimal)):
                    raise ValueError()
                rate = Decimal(raw)
                if not rate.is_finite() or rate <= 0:
                    raise ValueError()
                if currency in {'CNY', 'RMB'} and rate != 1:
                    raise ValueError()
                # 参考方向转换：内部外币/CNY = 1 / 来源CNY/外币。
                # 使用独立50位上下文；循环小数不提前舍入成价格，不改变domain业务公式。
                with localcontext(PRICE_CONTEXT):
                    inverse = str(Decimal(1) / rate)
            except (ValueError, InvalidOperation, ArithmeticError):
                error = '汇率缺失或不是有效正数/人民币基准无效'
        result.append({
            'currency': currency if isinstance(currency, str) else None,
            'source_rate': str(raw) if isinstance(raw, (str, int, Decimal)) and not isinstance(raw, bool) else None,
            'units_per_cny': inverse,
            'error': error,
        })
    return result


def query_erp_rates(settings: Settings, *, client=None):
    """明确调用才访问真实只读来源；不修改模式、执行历史或配置开关。"""
    base = settings.erp_open_api_url.rstrip('/')
    if base not in ALLOWED_BASES:
        raise SourceUnavailable('ERP开放接口地址不在已确认的公司服务范围内')
    token = settings.erp_token
    if not token and not (settings.erp_username and settings.erp_password.get_secret_value()):
        raise SourceUnavailable('请配置本项目独立的ERP账号密码或令牌')
    owned_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
    context = _private_request.set(True)
    try:
        if not token:
            login = _success(client.post(base + '/system/innerOpen/login', json={
                'username': settings.erp_username, 'password': settings.erp_password.get_secret_value(),
            }, timeout=TIMEOUT, follow_redirects=False))
            user = login.get('data')
            if not isinstance(user, dict):
                raise SourceUnavailable('ERP登录未返回有效令牌')
            token = user.get('accessToken') or user.get('token')
        if not isinstance(token, str) or not token.strip():
            raise SourceUnavailable('ERP登录未返回有效令牌')
        token = re.sub(r'^Bearer\s+', '', token, flags=re.IGNORECASE).strip()
        payload = _success(client.get(base + PATH, headers={'Authorization': 'Bearer ' + token},
                                      timeout=TIMEOUT, follow_redirects=False))
        if not isinstance(payload.get('data'), list) or payload.get('hasNext') is True:
            raise SourceUnavailable('ERP汇率列表格式无效或返回不完整')
        return {
            'source': 'company_erp', 'source_version': VERSION,
            'queried_at': datetime.now(timezone.utc).isoformat(),
            'effective_date': None, 'direction_confirmed': False,
            'assumed_source_unit': 'CNY_per_currency_unit',
            'pricing_ready': False,
            'message': '公司真实只读查询；汇率方向按实测推断，生效日期未提供，暂未用于自动改价。',
            'rows': _rows(payload['data']),
        }
    except (httpx.HTTPError, ValueError, TypeError, ArithmeticError):
        # 不回传上游原始响应、URL、认证头、个人信息和异常链。
        raise SourceUnavailable('ERP汇率查询失败或返回格式无效') from None
    finally:
        if owned_client:
            client.close()
        _private_request.reset(context)
