"""官方调价日志只读合同（2026-11-17实测），不提交价格、不参与72小时保护。

count按子商品ID/type=2/北京时间完成日期查询，不据此推断成功状态。
latest接口只有金额，没有币种、任务ID和时间；只有和对应明细priceNew比对后，
才能注明“本次金额与日志税前价一致”，不能把它直接当当前含税价或营销价。
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.normalization import identity_text, money
from app.adapters.sales_auth import SalesAuth, ORIGIN
from app.adapters.transport_privacy import private_request
from app.domain.engine import SITE_CURRENCIES
from app.domain.money import MoneyRatio
from app.domain.types import VariantKey
from app.services.erp_capture_store import write_verified

VERSION = 'official-price-log-20261117-v1'
COUNT_PATH = '/Api/tiktok-schedule-server/aibp/getOperationLogCountBySellerSkuIdsAndTypeAndCompleteTime'
LATEST_PATH = '/Api/tiktok-schedule-server/aibp/getLatestSuccessPrice'


def _now():
    return datetime.now(timezone.utc).isoformat()


def capture_official_price_logs(settings, key: VariantKey, shop_name: str, business_date: date,
                               directory, *, client=None):
    """单目标、两条查询独立留证。官方无记录/失败/缺字段和确证0条严格区分。"""
    if settings.vendor_api_url.rstrip('/') != ORIGIN:
        raise SourceUnavailable('官方日志来源地址未确认')
    identity_text(shop_name)
    if type(business_date) is not date:
        raise ValueError('官方日志完成日期须为明确业务日')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source_id = 'official-price-log-' + uuid4().hex
    result = {'schema_version': VERSION, 'source_id': source_id, 'key': key.model_dump(mode='json'),
              'shop_name': shop_name, 'queried_at': _now(), 'business_date': business_date.isoformat(),
              'archive_file': source_id+'.json', 'synthetic': False}
    day = business_date.isoformat()
    queries = [
        ('count_query', COUNT_PATH, {'sellerSkuIds': [key.item_id], 'type': 2,
            'completeTimeStart': day+' 00:00:00', 'completeTimeEnd': day+' 23:59:59'}),
        ('latest_success_query', LATEST_PATH, {'shopName': shop_name, 'productId': key.product_id, 'sellerSkuId': key.item_id})]
    owned = client is None
    client = client or httpx.Client(timeout=40, trust_env=False, follow_redirects=False)
    auth = SalesAuth(settings, client)
    context = private_request.set(True)
    try:
        for name, path, body in queries:
            record = {'method': 'POST', 'path': path, 'request': body, 'queried_at': _now(),
                      'status': 'unavailable', 'http_status': None, 'response_code': None}
            if name == 'count_query':
                record.update(count=None, success_status_filter_confirmed=False)
            else:
                record.update(amount=None, price_basis='unconfirmed', source_currency=None,
                              source_task_id=None, source_effective_at=None)
            try:
                response = client.post(ORIGIN+path, json=body,
                    headers={'Authorization': 'Bearer '+auth.get_token()}, timeout=40, follow_redirects=False)
                record.update(http_status=response.status_code, response_sha256=sha256(response.content).hexdigest())
                response.raise_for_status()
                value = response.json(parse_float=Decimal)
                if not isinstance(value, dict) or type(value.get('resp_code')) is not int:
                    raise ValueError('官方日志响应结构无效')
                record['response_code'] = value['resp_code']
                if value['resp_code'] != 0:
                    record['status'] = ('not_found' if name=='latest_success_query' and value['resp_code']==1
                        and value.get('resp_msg')=='未找到调价成功的记录' else 'business_error')
                elif name == 'count_query':
                    rows = value.get('datas')
                    if (not isinstance(rows, list) or len(rows)!=1 or not isinstance(rows[0],dict)
                            or rows[0].get('sellerSkuId')!=key.item_id or type(rows[0].get('count')) is not int
                            or rows[0]['count']<0):
                        raise ValueError('官方计数身份或数量无效')
                    record.update(status='ok', count=rows[0]['count'],
                        response={'resp_code':0,'datas':[{'sellerSkuId':key.item_id,'count':rows[0]['count']}]})
                else:
                    price = money(value.get('datas'))
                    if price<=0: raise ValueError('官方最新成功金额不是正数')
                    record.update(status='ok', amount=format(price,'f'), response={'resp_code':0,'datas':format(price,'f')})
            except (httpx.HTTPError, SourceUnavailable) as error:
                record.update(status='transport_error', error_kind=type(error).__name__)
            except (ValueError, TypeError, KeyError, ArithmeticError) as error:
                record.update(status='invalid_response', error_kind=type(error).__name__)
            record['completed_at'] = _now()
            result[name] = record
    finally:
        private_request.reset(context)
        if owned: client.close()
    result['completed_at'] = _now()
    # 原始字节哈希+已核对响应字段；不归档认证头、令牌或上游任意错误正文。
    write_verified(directory / result['archive_file'], result)
    return result


def build_price_record(execution_id, key, shop_name, uploaded_price, log, current,
                       observed_at, official, *, submitted_at=None):
    """四价分开：持久上传意图、调价日志税前价、平台当前含税价、销售当前营销价。

observed_at是本次完成回读的时间，不是平台源更新时间或执行生效时间。
金额不相同是业务事实，不能选一个字段覆盖其他口径。
"""
    if (log.get('shopName')!=shop_name or log.get('productId')!=key.product_id
            or log.get('sellerSkuId')!=key.item_id or log.get('type')!=2
            or official.get('key')!=key.model_dump(mode='json') or official.get('shop_name')!=shop_name
            or any(current.get(n)!=getattr(key,n) for n in ('shop_id','product_id','item_id'))
            or current.get('currency')!=SITE_CURRENCIES[key.site]
            or money(log['priceIncludeVat'])!=uploaded_price):
        raise ValueError('价格存档的执行目标、币种或上传意图不一致')
    if observed_at.tzinfo is None: raise ValueError('价格观察时间缺少时区')
    currency = current['currency']
    definitions = {
        'uploaded_tax_inclusive': (uploaded_price, 'execution.request.upload_price', 'local_execution_record'),
        'tax_exclusive': (log['priceNew'], 'operationLog.priceNew', 'sales_operation_log'),
        'actual_tax_inclusive': (current['retail'], 'platform.skus.price.sale_price / company.priceIncludeVat', 'current_platform_and_company'),
        'marketing': (current['actual'], 'company.marketPrice', 'current_company_variant')}
    prices = {}
    for name,(value,field,source) in definitions.items():
        amount = money(value)
        if amount<=0: raise ValueError('存档价格须为明确正数')
        prices[name] = {'amount':format(amount,'f'), 'currency':currency,
            'currency_source':'current_platform_and_company_identity', 'source':source, 'source_field':field,
            'observed_at':observed_at.isoformat(), 'time_basis':'local_read_observation'}
    prices['uploaded_tax_inclusive']['submitted_at'] = submitted_at
    latest = official['latest_success_query']
    comparison = 'unavailable'
    if latest.get('status')=='ok':
        comparison = ('matches_log_tax_exclusive' if money(latest['amount'])==money(log['priceNew'])
                      else 'different_from_log_tax_exclusive')
    return {'schema_version':'execution-price-record-v1', 'execution_id':execution_id,
        'key':key.model_dump(mode='json'),'shop_name':shop_name,'recorded_at':_now(),
        'observation_scope':'readback_at_observed_at_not_original_effective_time', 'prices':prices,
        'platform_tax_exclusive':format(money(current['tax_exclusive']),'f'),
        'logistics_cost':format(money(current['logistics']),'f'), 'discount_percent_off':str(current['discount']),
        'operation_log':{n:log.get(n) for n in ('id','type','status','operator','createTime','completeTime')},
        'operation_log_time_zone':'Asia/Shanghai', 'official_queries':official,'official_comparison':comparison,
        'official_comparison_limit':'金额相同不单独证明官方latest记录绑定本次执行；接口未返回任务ID及币种时间',
        'differences':{'actual_tax_inclusive_minus_uploaded':str((MoneyRatio(money(current['retail']))-MoneyRatio(uploaded_price)).decimal())}}
