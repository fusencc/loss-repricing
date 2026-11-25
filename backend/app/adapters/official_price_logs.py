"""官方调价日志只读合同（2026-11-17实测），不提交价格；近72小时保护使用官方计数。

count按子商品ID/type=2/北京时间完成日期查询，不据此推断成功状态。
latest接口只有金额，没有币种、任务ID和时间；只有和对应明细priceNew比对后，
才能注明“本次金额与日志税前价一致”，不能把它直接当当前含税价或营销价。
"""
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.normalization import identity_text, money
from app.adapters.sales_auth import SalesAuth, ORIGIN
from app.adapters.transport_privacy import private_request
from app.domain.engine import BEIJING, SITE_CURRENCIES
from app.domain.money import MoneyRatio
from app.domain.types import VariantKey, RecentPriceLog
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
            or (official is not None and (official.get('key')!=key.model_dump(mode='json')
                or official.get('shop_name')!=shop_name))
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
        'marketing': (current['actual'], current.get('marketing_source_field', 'company.marketPrice'),
                      'current_company_variant')}
    prices = {}
    for name,(value,field,source) in definitions.items():
        amount = money(value)
        if amount<=0: raise ValueError('存档价格须为明确正数')
        prices[name] = {'amount':format(amount,'f'), 'currency':currency,
            'currency_source':'current_platform_and_company_identity', 'source':source, 'source_field':field,
            'observed_at':observed_at.isoformat(), 'time_basis':'local_read_observation'}
    prices['uploaded_tax_inclusive']['submitted_at'] = submitted_at
    latest = (official or {}).get('latest_success_query', {})
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


def capture_recent_price_changes(settings, keys, *, now=None, client=None, auth=None):
    """11/17用户指定：滚动72小时内type=2日志大于0即保护，不限操作者或涨跌。

    完成时间按北京时间秒精度传入；不臆造成功状态过滤或最近生效时间。
    批量按子商品ID查询，缺行/重复/异常不等于0；独立SKU结果分别保留。
    """
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        raise ValueError('调价日志查询时间缺少时区')
    end = end.astimezone(BEIJING).replace(microsecond=0)
    start = end - timedelta(hours=72)
    ids = sorted({identity_text(key.item_id) for key in keys})
    source_id = 'official-recent-price-' + uuid4().hex
    path = Path(settings.runtime_dir) / 'official-price-history' / (source_id + '.json')
    result = {'schema_version':'official-recent-price-v1','source_id':source_id,
        'window_start':start.isoformat(),'window_end':end.isoformat(),'queried_at':_now(),
        'basis':'official_type_2_completion_time','synthetic':False,'success_status_filter_confirmed':False,
        'records':{item:{'status':'unavailable','count':None} for item in ids},
        'queries':[],'artifact_file':str(path.resolve())}
    owned = client is None
    client = client or httpx.Client(timeout=40,trust_env=False,follow_redirects=False)
    auth = auth or SalesAuth(settings,client)
    context = private_request.set(True)
    try:
        for offset in range(0,len(ids),100):
            batch = ids[offset:offset+100]
            body = {'sellerSkuIds':batch,'type':2,'completeTimeStart':start.strftime('%Y-%m-%d %H:%M:%S'),
                'completeTimeEnd':end.strftime('%Y-%m-%d %H:%M:%S')}
            query = {'method':'POST','path':COUNT_PATH,'request':body,'status':'unavailable'}
            stop = False
            try:
                if settings.vendor_api_url.rstrip('/') != ORIGIN:
                    raise SourceUnavailable('官方日志来源地址未确认')
                response = client.post(ORIGIN+COUNT_PATH,json=body,
                    headers=auth.collection_headers(),timeout=40,follow_redirects=False)
                auth.observe_collection_response(response)
                query.update(http_status=response.status_code,response_sha256=sha256(response.content).hexdigest())
                response.raise_for_status()
                value = response.json()
                if not isinstance(value,dict) or type(value.get('resp_code')) is not int:
                    raise ValueError('官方日志响应无效')
                query['response_code'] = value['resp_code']
                if value['resp_code'] != 0:
                    query['status'] = 'business_error'
                else:
                    rows = value.get('datas')
                    if not isinstance(rows,list):raise ValueError('官方日志列表无效')
                    query['status'] = 'ok'
                    for item in batch:
                        matches = [r for r in rows if isinstance(r,dict) and r.get('sellerSkuId')==item]
                        valid = (len(matches)==1 and type(matches[0].get('count')) is int and matches[0]['count']>=0)
                        result['records'][item] = {'status':'ok' if valid else 'invalid_response',
                            'count':matches[0]['count'] if valid else None}
            except (httpx.HTTPError,SourceUnavailable) as error:
                query.update(status='transport_error',error_kind=type(error).__name__)
                stop = isinstance(error, SourceUnavailable)
            except (ValueError,TypeError,KeyError) as error:
                query.update(status='invalid_response',error_kind=type(error).__name__)
            result['queries'].append(query)
            if auth.stopped_http_status is not None:
                result['stopped_http_status'] = auth.stopped_http_status
                break
            if query['status']!='ok':
                for item in batch:result['records'][item]={'status':query['status'],'count':None}
            if stop:
                break
    finally:
        private_request.reset(context)
        if owned:client.close()
    result['completed_at'] = _now()
    result['acquisition_complete'] = all(r['status']=='ok' for r in result['records'].values())
    write_verified(path,result)
    return result


def recent_price_log_for(source, key):
    """只接受当前目标明确返回的计数，未知不返回零。"""
    row = source['records'].get(key.item_id)
    if not row or row.get('status')!='ok':
        raise ValueError('近72小时开放调价日志查询失败或缺少本子SKU结果')
    recent = RecentPriceLog(count=row['count'],window_start=source['window_start'],
        window_end=source['window_end'],source_id=source['source_id'])
    if not recent.source_id or recent.window_end-recent.window_start != timedelta(hours=72):
        raise ValueError('开放调价日志来源窗口不是72小时')
    return recent
