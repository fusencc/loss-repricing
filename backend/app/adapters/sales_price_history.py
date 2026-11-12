"""当前销售操作日志原件；公司日志完整性与平台后台手动改价覆盖性分开记录。

合同：前端13924 operationLog/findByPage，任务类型必选、店铺多值空格分隔。
日期控件经handelParams补齐00:00:00/23:59:59；完整读完覆盖窗口的自然日，
再在客户端按北京时间精确筛72小时。没有无type的查询或旧合同回退。
本来源仅收集已经完成时间范围内的公司记录。用户2026-11-11已明确暂缓
TK后台自行改价覆盖，保护范围先采用公司日志及本程序已确认生效的存档。
本来源单独不声明合并历史已确认，也不将待提价或公司成功状态冒充平台生效。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4
from time import sleep

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.sales_read import read_response
from app.adapters.sales_auth import ORIGIN, SalesAuth
from app.adapters.sales_capture import _json_values
from app.adapters.transport_privacy import private_request
from app.domain.engine import BEIJING
from app.services.erp_capture_store import write_verified

PATH = '/Api/api-tiktok-sales/operationLog/findByPage'
# 前端当前完整任务类型枚举。库存/活动任务也可能携带价格，不按名称漏掉。
OPERATION_TYPES = (*range(1, 20), *range(23, 28))


def capture_price_history(settings, sales, *, client=None, observed_at=None, page_size=100,
                          shop_batch_size=50, progress=None, not_before=None, shop_scope=None):
    if settings.vendor_api_url.rstrip('/') != ORIGIN:
        raise SourceUnavailable('销售日志来源地址未确认')
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError('日志页大小须为1至1000')
    if type(shop_batch_size) is not int or not 1 <= shop_batch_size <= 50:
        raise ValueError('日志店铺批大小须为1至50')
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('日志观察时点须有时区')
    if not_before is not None:
        if not_before.tzinfo is None:
            raise ValueError('价格观察边界须有时区')
        if observed_at is not None:
            if now.replace(microsecond=0)<not_before:
                raise ValueError('固定日志cutoff早于价格观察')
        elif now.replace(microsecond=0)<not_before:
            if not_before>now:
                raise SourceUnavailable('价格观察时点晚于当前时钟，不能填写未来日志窗口')
            boundary=not_before.replace(microsecond=0)+timedelta(seconds=1)
            sleep((boundary-now).total_seconds())
            now=datetime.now(timezone.utc)
            if now.replace(microsecond=0)<not_before:
                raise SourceUnavailable('实际时钟尚未覆盖价格观察，不能确认历史')
    end = now.astimezone(BEIJING).replace(microsecond=0)
    start = end - timedelta(hours=72)
    captured_shops = {row['shopName'] for row in sales['products']}
    if shop_scope is not None and (not isinstance(shop_scope, (list, tuple, set))
            or any(not isinstance(shop, str) for shop in shop_scope)
            or not set(shop_scope).issubset(captured_shops)):
        raise ValueError('日志店铺范围须来自本轮销售来源')
    shops = sorted(captured_shops if shop_scope is None else set(shop_scope))
    unqueryable = [shop for shop in shops if not isinstance(shop, str) or not shop
                   or any(char.isspace() for char in shop)]
    source_id = 'sales-price-history-' + uuid4().hex
    directory = Path(settings.runtime_dir) / 'sales-price-history' / source_id
    directory.mkdir(mode=0o700, parents=True)
    result = {'schema_version':'sales-company-price-history-v1','source_id':source_id,
        'erp_capture_id':sales['erp_capture_id'],'sales_source_id':sales['source_id'],
        'synthetic':sales['synthetic'],'sample_only':sales['sample_only'],
        'queried_at':now.isoformat(),'window_start':start.isoformat(),'window_end':end.isoformat(),
        'scope_policy':'captured_shops' if shop_scope is None else 'retained_pricing_shops',
        'query_timezone':'Asia/Shanghai','requested_shops':shops,'records':[],'files':[], 'issues':[], 'read_retries':[],
        'query_start_date':str(start.date()),'query_end_date':str(end.date()),
        'requested_types':list(OPERATION_TYPES),
        'source_record_count':0,'excluded_time_records':[],
        'company_log_complete':False,'platform_manual_changes_complete':False,'pricing_ready':False,
        'protection_scope':'company_log_and_local_verified_archive',
        'platform_manual_policy':'deferred_by_user_20261111',
        'coverage_note':'用户暂缓TK后台自行改价覆盖；须与本程序已确认生效存档合并后判断72小时保护',
        'artifact_file':str((directory / 'result.json').resolve())}
    if unqueryable:
        result['issues'].append({'code':'history_shop_not_queryable','shops':unqueryable})
    shops = [shop for shop in shops if shop not in unqueryable]
    owned = client is None
    client = client or httpx.Client(timeout=40, trust_env=False, follow_redirects=False)
    auth = SalesAuth(settings, client)
    context = private_request.set(True)
    seen = set()
    try:
        plan = [(shops[offset:offset+shop_batch_size], kind)
                for offset in range(0, len(shops), shop_batch_size) for kind in OPERATION_TYPES]
        for number, (batch, kind) in enumerate(plan, 1):
            page, total, collected = 1, None, []
            try:
                while True:
                    if progress:
                        progress(f'读取公司72小时操作日志：第{number}/{len(plan)}批，类型{kind}，第{page}页。')
                    body = {'page':page,'limit':page_size,'type':kind,'shopNames':' '.join(batch),
                        'completeTimeStart':str(start.date())+' 00:00:00',
                        'completeTimeEnd':str(end.date())+' 23:59:59'}
                    token=auth.get_token()
                    def archive(response,attempt):
                        path = directory / f'{len(result["files"])+1:05d}.json'
                        path.write_bytes(response.content)
                        raw = path.read_bytes()
                        if raw != response.content:
                            raise OSError('操作日志原件回读不一致')
                        result['files'].append({'file':str(path.resolve()),'sha256':sha256(raw).hexdigest(),
                            'bytes':len(raw),'request':body,'attempt':attempt,'http_status':response.status_code,
                            'received_at':datetime.now(timezone.utc).isoformat()})
                    response=read_response(lambda:client.post(ORIGIN+PATH,json=body,
                        headers={'Authorization':'Bearer '+token},timeout=40,follow_redirects=False),archive,
                        retry_events=result['read_retries'],stage='company_history')
                    response.raise_for_status()
                    payload = response.json(parse_float=Decimal)
                    if not isinstance(payload, dict):
                        raise ValueError('history_response_invalid')
                    if type(payload.get('resp_code')) is not int or payload['resp_code'] != 0:
                        raise ValueError('history_business_failed')
                    container = payload.get('datas')
                    if not isinstance(container, dict):
                        raise ValueError('history_response_invalid')
                    rows, count = container.get('data'), container.get('count')
                    if not isinstance(rows, list) or type(count) is not int or count < 0:
                        raise ValueError('history_pagination_incomplete')
                    if total is None:
                        total = count
                    if total != count or len(rows) != min(page_size, max(0, total - len(collected))):
                        raise ValueError('history_pagination_incomplete')
                    for row in rows:
                        if (not isinstance(row, dict) or row.get('shopName') not in batch
                                or type(row.get('type')) is not int or row['type'] != kind):
                            raise ValueError('history_scope_mismatch')
                        try:
                            stamp = datetime.strptime(row.get('completeTime'), '%Y-%m-%d %H:%M:%S').replace(tzinfo=BEIJING)
                        except (TypeError, ValueError):
                            raise ValueError('history_scope_mismatch') from None
                        if not start.date() <= stamp.date() <= end.date():
                            raise ValueError('history_scope_mismatch')
                        identity = row.get('id')
                        if (not isinstance(identity, (str, int)) or isinstance(identity, bool)
                                or not str(identity).strip() or str(identity) in seen):
                            raise ValueError('history_pagination_incomplete')
                        seen.add(str(identity))
                    collected.extend(rows)
                    if len(collected) == total:
                        result['source_record_count'] += len(collected)
                        for row in collected:
                            stamp = datetime.strptime(row['completeTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BEIJING)
                            if start <= stamp <= end:
                                result['records'].append(row)
                            else:
                                result['excluded_time_records'].append({'id':row['id'],'completeTime':row['completeTime']})
                        break
                    page += 1
            except (SourceUnavailable, httpx.HTTPError, ValueError) as error:
                code = ('history_authentication_failed' if isinstance(error, SourceUnavailable) else
                        str(error) if type(error) is ValueError and str(error).startswith('history_') else 'history_read_failed')
                error_kind = ('timeout' if isinstance(error, httpx.TimeoutException) else
                        'transport' if isinstance(error, httpx.RequestError) else
                        'http' if isinstance(error, httpx.HTTPStatusError) else
                        'authentication' if isinstance(error, SourceUnavailable) else 'response')
                result['issues'].append({'code':code,'shops':batch,'type':kind,'error_kind':error_kind})
                if isinstance(error, SourceUnavailable) or (
                        isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403)):
                    break
    finally:
        private_request.reset(context)
        if owned:
            client.close()
    result['company_log_complete'] = not result['issues']
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    result = _json_values(result)
    write_verified(result['artifact_file'], result)
    return result
