"""销售在线子 SKU 批量导出，供 ERP 部门候选取商品身份及算价字段。"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import json
from time import monotonic, sleep
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.erp_export_mapping import _product_ids, _text, _xlsx_rows
from app.adapters.erp_order_loss import _number
from app.adapters.normalization import sku_text
from app.adapters.sales_auth import COLLECTION_STOP_STATUSES, ORIGIN, SalesAuth
from app.adapters.sales_capture import CURRENCY_RATES, _json_values
from app.adapters.transport_privacy import private_request
from app.services.erp_capture_store import write_verified

EXPORT = '/Api/api-tiktok-sales/online/export'
TASKS = '/Api/api-tiktok-sales/exportTask/findByPage'
BATCH_SIZE = 350
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
FIELDS = (
    '产品ID', '商品ID', '本地主sku', '本地子sku', '平台子sku', '币种',
    '7天销量', '税前价', '含vat价格', '营销价格', '折扣', '活动类型',
    'VAT费率', '平台尾类目ID', '直发公式利润率', '海外仓公式利润率',
    '海外仓库存', '海外仓仓库', 'erp成本价', '包装重量',
)
COLUMNS = ('集团', '部门', '店铺名称', '站点', *(
    '海外仓公式利润率(%)' if field == '海外仓公式利润率' else field for field in FIELDS
))


def select_export_candidates(capture, verified_orders):
    """同订单多来源必须一致，保留不同亏损订单，商品请求再去重。"""
    observations = defaultdict(list)
    for group in capture['groups']:
        for row in group['rows']:
            if row['orderid'] in verified_orders:
                observations[row['orderid']].append(row['fields'])
    candidates = {}
    for order, rows in observations.items():
        try:
            values = {(_number(row.get('修正一次毛利')), _product_ids(row.get('item ID'))) for row in rows}
        except ValueError:
            continue
        if len(values) == 1:
            margin, products = next(iter(values))
            if margin < 0 and products:
                candidates[order] = tuple(sorted(products))
    return candidates


def export_activity_hints(sales):
    """同一店铺父商品任一子 SKU 有活动即筛有活动侧；价格字段不完整则让现态查两侧。"""
    states = defaultdict(set)
    for variant in sales['variants']:
        row = variant['batch_pricing']
        active = True if row['活动类型'] else (
            False if row['折扣'] in (None, '') and row['营销价格'] in (None, '') else None
        )
        states[variant['shopName'], variant['productId']].add(active)
    return {key: True in values for key, values in states.items() if True in values or values == {False}}


def _parse_export(content, ids, bloc):
    rows = list(_xlsx_rows(content, COLUMNS, '销售子SKU导出'))
    if not rows:
        raise ValueError('sales_export_empty')
    for _, row in rows:
        product = row['产品ID']
        if not isinstance(product, str) or product not in ids or row['集团'] != bloc:
            raise ValueError('sales_export_outside_request')
    return rows


def _export_file(client, auth, ids, bloc, *, checkpoint=None):
    """只创建一次导出；临时查询失败继续跟踪，任务证据与成功文件分开落盘。"""
    task_id, previous = None, set()
    state = {'state': 'not_submitted', 'task_id': None, 'previous_task_ids': [],
             'product_ids': list(ids), 'bloc': bloc, 'read_failures': []}

    def save(**values):
        state.update(values, task_id=str(task_id) if task_id is not None else None,
                     previous_task_ids=sorted(previous))
        if checkpoint is not None:
            checkpoint(state)

    def post(path, body):
        response = client.post(ORIGIN + path, json=body, headers=auth.collection_headers(),
                               timeout=30, follow_redirects=False)
        if response.status_code in COLLECTION_STOP_STATUSES:
            auth.observe_collection_response(response)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get('resp_code') != 0:
            error = ValueError('sales_export_business_failed')
            code = data.get('resp_code') if isinstance(data, dict) else None
            error.business_code = code if type(code) is int or (isinstance(code, str) and len(code) <= 64) else None
            raise error
        return data.get('datas')

    def read(call, stage):
        # 查询和下载是幂等读取；HTTP 400/5xx、超时和业务失败不证明源端
        # 导出任务失败。保留原任务，限时重读；认证/权限/限流立即停止。
        deadline = monotonic() + 90
        for attempt in range(19):
            try:
                return call()
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
                status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
                failure = {'stage': stage, 'reason': type(error).__name__,
                           'http_status': status, 'business_code': getattr(error, 'business_code', None)}
                state['read_failures'].append(failure)
                # 只保留受控类型、状态及业务码，不复制响应正文、凭据或下载地址。
                save(state='poll_retry' if stage != 'download' else 'download_retry', stage=stage)
                if (status in COLLECTION_STOP_STATUSES or attempt == 18 or monotonic() >= deadline
                        or (isinstance(error, ValueError) and str(error) == 'sales_export_file_too_large')):
                    save(state='read_failed', stage=stage)
                    raise
                sleep(5)

    def tasks():
        # 11/25 真实任务页已验证支持100条；扩大观察窗口，减少并发导出挤出本任务。
        return read(lambda: post(TASKS, {'page': 1, 'limit': 100})['data'], 'task_query')

    previous = {str(row['id']) for row in tasks()}
    save(state='submitting', stage='submit')
    try:
        submitted = post(EXPORT, {'blocNames': [bloc], 'productIdStr': ' '.join(ids),
                                  'exportType': 2, 'exportField': list(FIELDS)})
    except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError) as error:
        # 创建请求的结果不明确时不自动重发；保留提交前任务集合供后续核对。
        save(state='submission_unconfirmed', stage='submit',
             reason=type(error).__name__, business_code=getattr(error, 'business_code', None),
             http_status=error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None)
        raise
    task_id = submitted.get('id') or submitted.get('taskId') if isinstance(submitted, dict) else None
    save(state='accepted', stage='task_query')
    deadline = monotonic() + 90
    while monotonic() < deadline:
        candidates = [
            row for row in tasks()
            if row.get('taskType') == 1 and (
                str(row['id']) == str(task_id) if task_id else str(row['id']) not in previous
            )
        ]
        if len(candidates) > 1:
            raise ValueError('sales_export_task_ambiguous')
        if candidates:
            task = candidates[0]
            task_id = task['id']
            save(state='bound', stage='task_query')
            if task.get('status') == 4:
                raise ValueError('sales_export_task_failed')
            if task.get('status') == 3:
                url = urlsplit(task['excelUrl'])
                if (url.scheme, url.hostname, url.port) != ('http', '121.46.237.218', 19110) or url.username:
                    raise ValueError('sales_export_download_origin_unverified')
                def download():
                    content = bytearray()
                    auth.wait_for_request()
                    with client.stream('GET', task['excelUrl'], timeout=30, follow_redirects=False) as response:
                        if response.status_code in COLLECTION_STOP_STATUSES:
                            auth.observe_collection_response(response)
                        response.raise_for_status()
                        for chunk in response.iter_bytes():
                            content.extend(chunk)
                            if len(content) > MAX_DOWNLOAD_BYTES:
                                raise ValueError('sales_export_file_too_large')
                    return bytes(content)
                content = read(download, 'download')
                # 提交实测 datas=null。唯一新增任务还必须用文件内的完整产品集合核对，
                # 避免并发时把他人的文件认成本批，缺失范围不进入本批算价。
                rows = _parse_export(content, set(ids), bloc)
                if {row['产品ID'] for _, row in rows} != set(ids):
                    raise ValueError('sales_export_product_coverage_incomplete')
                save(state='downloaded', stage='complete', rows=len(rows), bytes=len(content))
                return content, task_id, rows
        sleep(5)
    save(state='pending', stage='task_query')
    raise ValueError('sales_export_timeout')


def capture_sales_batch(settings, capture, candidates, shop_scopes, *, client=None, auth=None, progress=None):
    if settings.vendor_api_url.rstrip('/') != ORIGIN:
        raise SourceUnavailable('销售导出地址未确认')
    shops_by_product = defaultdict(lambda: defaultdict(set))
    for order, products in candidates.items():
        scope = shop_scopes.get(order)
        if scope:
            for product in products:
                shops_by_product[product][scope['erp_shop']].add(scope['shop_id'])
    ids = sorted(shops_by_product)
    source_id = 'sales-batch-export-' + uuid4().hex
    directory = Path(settings.runtime_dir) / 'sales-source-captures' / source_id
    directory.mkdir(mode=0o700, parents=True)
    started = datetime.now(timezone.utc).isoformat()
    parents, variants, files, rates = [], [], [], []
    export_tasks = []
    issues = [
        {'code': 'missing_product_hint', 'order_id': row['orderid']}
        for group in capture['groups'] for row in group['rows']
        if not _product_ids(row['fields'].get('item ID'))
    ]
    owned = client is None
    client = client or httpx.Client(timeout=30, trust_env=False, follow_redirects=False)
    auth = auth or SalesAuth(settings, client)
    context = private_request.set(True)
    def persist(*, completed=False):
        result = {
            'schema_version': 'sales-source-observations-v1', 'source_id': source_id,
            'erp_capture_id': capture['capture_id'], 'synthetic': capture.get('synthetic', False),
            'sample_only': capture.get('sample_only', False), 'source_mode': 'online_sku_batch_export',
            'queried_at': started, 'completed_at': datetime.now(timezone.utc).isoformat() if completed else None,
            'products': parents, 'variants': variants, 'files': files, 'issues': issues,
            'export_tasks': export_tasks,
            'currency_rates': rates, 'currency_rate_unit': 'cny_per_currency',
            'acquisition_complete': completed and not issues,
            'summary': {'requested_products': len(ids), 'products': len(parents), 'variants': len(variants)},
            'artifact_file': str((directory / 'result.json').resolve()),
        }
        write_verified(result['artifact_file'], result)
        return result
    try:
        persist()
        for offset in range(0, len(ids), BATCH_SIZE):
            batch = ids[offset:offset+BATCH_SIZE]
            if progress:
                progress(f'批量导出销售子SKU：第 {offset // BATCH_SIZE + 1} 批，{len(batch)} 个产品。')
            task_file = directory / f'{offset // BATCH_SIZE + 1:03d}-task.json'
            export_tasks.append(str(task_file))
            def checkpoint(state):
                write_verified(task_file, state)
            try:
                content, task_id, rows = _export_file(client, auth, batch, settings.erp_capture_bloc,
                                                      checkpoint=checkpoint)
            except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError) as error:
                issue = {'code': 'sales_batch_unavailable', 'product_ids': batch,
                         'reason': str(error) if isinstance(error, ValueError) else type(error).__name__}
                status = (error.response.status_code if isinstance(error, httpx.HTTPStatusError)
                          else auth.stopped_http_status)
                if status is not None:
                    issue['http_status'] = status
                if task_file.exists():
                    saved_task = json.loads(task_file.read_text())
                    issue.update(stage=saved_task.get('stage'), task_id=saved_task.get('task_id'),
                                 task_evidence=str(task_file))
                issues.append(issue)
                persist()
                if isinstance(error, SourceUnavailable) or status in COLLECTION_STOP_STATUSES:
                    break
                continue
            path = directory / f'{offset // BATCH_SIZE + 1:03d}.xlsx'
            path.write_bytes(content)
            files.append({'file': str(path), 'sha256': sha256(content).hexdigest(), 'task_id': str(task_id)})
            matched = set()
            for line, row in rows:
                shop, product = row['店铺名称'], row['产品ID']
                shop_ids = shops_by_product[product].get(shop, set())
                if (len(shop_ids) != 1 or not _text(row['商品ID']) or not _text(row['站点'])
                        or any(not _text(row[key], sku_text) for key in ('本地主sku', '本地子sku', '平台子sku'))):
                    continue
                matched.add((product, shop))
                parent = {'shopName': shop, 'shopId': next(iter(shop_ids)), 'productId': product,
                          'bloc': row['集团'], 'localMainSku': row['本地主sku'], 'exportSite': row['站点']}
                if parent not in parents:
                    parents.append(parent)
                variants.append({**parent, 'sellerSkuId': row['商品ID'], 'localSellerSku': row['本地子sku'],
                                 'sellerSku': row['平台子sku'],
                                 'batch_pricing': {key: str(value) if type(value) is float else value
                                                   for key, value in row.items()},
                                 'export_ref': f'{source_id}/{path.name}:{line}'})
            for product in batch:
                for shop in shops_by_product[product]:
                    if (product, shop) not in matched:
                        issues.append({'code': 'sales_export_shop_product_missing', 'product_id': product,
                                       'shop': shop})
            persist()
        if variants and auth.stopped_http_status is None:
            try:
                response = client.get(ORIGIN + CURRENCY_RATES, headers=auth.collection_headers(),
                                      timeout=30, follow_redirects=False)
                if response.status_code == 429:
                    auth.observe_collection_response(response)
                response.raise_for_status()
                raw = response.json(parse_float=Decimal)
                if not isinstance(raw, list):
                    raise ValueError('sales_currency_rates_invalid')
                rates = _json_values(raw)
            except (SourceUnavailable, httpx.HTTPError, ValueError) as error:
                issues.append({'code': 'sales_currency_rate_unavailable', 'reason': type(error).__name__})
    finally:
        private_request.reset(context)
        if owned:
            client.close()
    return persist(completed=True)

