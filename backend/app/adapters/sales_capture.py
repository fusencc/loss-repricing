"""按ERP父产品候选读取销售来源原件；不把原始字段冒充完整定价快照。

合同依据：当前销售前端13924及2026-11-11真实只读响应。父产品分页、
逐父产品展开变体、authType=2部门配置及globalPublish=false区域公共费用均已核对。
接口字段仍保留原币、百分数及空值；这里不推断销量窗口、保护或费用模型审核状态。
"""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.sales_read import read_response
from app.adapters.erp_export_mapping import _product_ids
from app.adapters.sales_auth import ORIGIN, SalesAuth
from app.adapters.sales_contracts import SHOP_SITE_CONTRACT, site_from_shop_name
from app.adapters.normalization import money
from app.adapters.transport_privacy import private_request
from app.domain.engine import SITE_CURRENCIES
from app.services.erp_capture_store import write_verified


PRODUCTS = '/Api/api-tiktok-sales/online/findByPage'
VARIANTS = '/Api/api-tiktok-sales/online/findProductVariants/'
DEPARTMENT_RATES = '/Api/api-tiktok-sales/supplierrate/findByPage'
OVERSEAS_RATES = '/Api/api-tiktok-sales/supplierrateoversea/findByPage'
PUBLIC_RATES = '/Api/api-tiktok-publish/autoPublishConfig/getPublicRatesByShopName'
CURRENCY_RATES = '/Api/api-b/rate/findAll'


class _Issue(Exception):
    def __init__(self, code):
        self.code = code


def _json_values(value):
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, dict):
        return {key: _json_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_values(item) for item in value]
    return value


def _identity(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool) and bool(str(value).strip())


def capture_sales_sources(settings, capture, *, client=None, page_size=100, product_batch_size=50, progress=None):
    if settings.vendor_api_url.rstrip('/') != ORIGIN:
        raise SourceUnavailable('销售来源地址尚未确认')
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError('销售来源页大小须为1至1000')
    if type(product_batch_size) is not int or not 1 <= product_batch_size <= 50:
        raise ValueError('销售父产品分组须为1至50')
    products_requested, missing_hints = set(), set()
    for group in capture['groups']:
        for row in group['rows']:
            hints = _product_ids(row['fields'].get('item ID'))
            products_requested.update(hints)
            if not hints:
                missing_hints.add(row['orderid'])
    requested = sorted(products_requested)
    source_id = 'sales-capture-' + uuid4().hex
    directory = Path(settings.runtime_dir) / 'sales-source-captures' / source_id
    directory.mkdir(mode=0o700, parents=True)
    result = {
        'schema_version':'sales-source-observations-v1', 'source_id':source_id,
        'erp_capture_id':capture['capture_id'], 'synthetic':bool(capture.get('synthetic', False)),
        'sample_only':bool(capture.get('sample_only', False)),
        'queried_at':datetime.now(timezone.utc).isoformat(), 'pricing_ready':False,
        'requested_product_ids':requested, 'missing_product_hint_orders':sorted(missing_hints),
        'products':[], 'variants':[], 'department_rates':{}, 'overseas_rates':[], 'public_rates':{}, 'issues':[], 'files':[],
        'currency_rates':[], 'currency_rate_unit':'cny_per_currency', 'read_retries':[],
        'shop_sites':{}, 'shop_site_contract':SHOP_SITE_CONTRACT,
    }
    owned = client is None
    client = client or httpx.Client(timeout=40, trust_env=False, follow_redirects=False)
    auth = SalesAuth(settings, client)
    unavailable = False
    context = private_request.set(True)

    def issue(stage, code, **scope):
        result['issues'].append({'stage':stage, 'code':code, **scope})

    for order_id in sorted(missing_hints):
        issue('scope', 'missing_product_hint', order_id=order_id)

    def request(stage, path, *, body=None, params=None):
        nonlocal unavailable
        if unavailable:
            raise _Issue('source_not_attempted')
        try:
            token = auth.get_token()
        except (httpx.HTTPError, SourceUnavailable):
            unavailable = True
            raise _Issue('source_authentication_failed') from None
        def archive(response,attempt):
            raw_path = directory / f'{len(result["files"])+1:05d}-{stage}.json'
            raw_path.write_bytes(response.content)
            restored = raw_path.read_bytes()
            if restored != response.content:
                raise OSError('销售来源原件回读不一致')
            result['files'].append({
                'file':str(raw_path.resolve()), 'bytes':len(restored), 'sha256':sha256(restored).hexdigest(),
                'stage':stage, 'attempt':attempt, 'path':path, 'request':params if params is not None else body,
                'http_status':response.status_code, 'received_at':datetime.now(timezone.utc).isoformat(),
            })
        try:
            response = read_response(lambda: client.request('GET' if params is not None else 'POST', ORIGIN + path,
                headers={'Authorization':'Bearer ' + token},
                **({'params':params} if params is not None else {'json':body}),
                timeout=40, follow_redirects=False),archive,retry_events=result['read_retries'],stage=stage)
        except (httpx.HTTPError, SourceUnavailable):
            raise _Issue('source_request_failed') from None
        if response.status_code in (401, 403):
            unavailable = True
        if not 200 <= response.status_code < 300:
            raise _Issue('source_http_failed')
        try:
            payload = response.json(parse_float=Decimal)
        except ValueError:
            raise _Issue('source_response_invalid') from None
        if path == CURRENCY_RATES:
            if (not isinstance(payload, list) or any(not isinstance(row, dict)
                    or not _identity(row.get('id')) or not isinstance(row.get('name'), str)
                    or not row['name'].strip() for row in payload)
                    or len({str(row['id']) for row in payload}) != len(payload)):
                raise _Issue('currency_rate_response_invalid')
            return payload
        code_field = 'code' if path in (PRODUCTS, OVERSEAS_RATES) else 'resp_code'
        if not isinstance(payload, dict) or type(payload.get(code_field)) is not int or payload[code_field] != 0:
            raise _Issue('source_business_failed')
        return payload

    def pages(stage, path, filters):
        rows, seen, total = [], set(), None
        page = 1
        while True:
            payload = request(stage, path, body={**filters, 'page':page, 'limit':page_size})
            container = payload if path in (PRODUCTS, OVERSEAS_RATES) else payload.get('datas')
            if not isinstance(container, dict):
                raise _Issue(stage + '_pagination_incomplete')
            records, count = container.get('data'), container.get('count')
            if not isinstance(records, list) or type(count) is not int or count < 0:
                raise _Issue(stage + '_pagination_incomplete')
            if total is None:
                total = count
            if count != total or len(records) != min(page_size, max(0, total - len(rows))):
                raise _Issue(stage + '_pagination_incomplete')
            for row in records:
                if not isinstance(row, dict) or not _identity(row.get('id')) or str(row['id']) in seen:
                    raise _Issue(stage + '_pagination_incomplete')
                seen.add(str(row['id']))
                rows.append(row)
            if len(rows) == total:
                return rows
            page += 1

    try:
        for offset in range(0, len(requested), product_batch_size):
            ids = requested[offset:offset+product_batch_size]
            batch = offset // product_batch_size + 1
            if progress:
                progress(f'读取销售在线父产品：第{batch}组，共{len(ids)}个候选。')
            try:
                parents = pages('product', PRODUCTS, {'blocNames':[settings.erp_capture_bloc], 'productIdStr':' '.join(ids)})
                if any(row.get('productId') not in ids or row.get('bloc') != settings.erp_capture_bloc
                       or not isinstance(row.get('shopName'), str) or not row['shopName'].strip() for row in parents):
                    raise _Issue('product_scope_mismatch')
            except _Issue as error:
                issue('product', error.code, batch=batch)
                continue
            result['products'].extend(parents)
            if not parents:
                continue
            for parent_number, parent in enumerate(parents, 1):
                target = (parent['shopName'], parent['productId'])
                if progress:
                    progress(f'读取销售当前变体：第{batch}组 {parent_number}/{len(parents)} 个父产品。')
                try:
                    path = VARIANTS + '/'.join(quote(value, safe='') for value in target)
                    payload = request('variant', path, body={'shopName':target[0], 'productId':target[1]})
                    variants = payload.get('datas')
                    if not isinstance(variants, list) or any(not isinstance(row, dict)
                        or (row.get('shopName'), row.get('productId')) != target for row in variants):
                        raise _Issue('variant_scope_mismatch')
                    for row in variants:
                        if not _identity(row.get('sellerSkuId')):
                            issue('variant', 'invalid_variant_identity', product_id=target[1], shop=target[0])
                        else:
                            result['variants'].append(row)
                except _Issue as error:
                    issue('variant', error.code, batch=batch, product_id=target[1], shop=target[0])

        sites = set()
        for product in result['products']:
            site = site_from_shop_name(product['shopName'])
            if site is None:
                issue('overseas_rate', 'unknown_shop_site', product_id=product['productId'], shop=product['shopName'])
            else:
                sites.add(site)
                result['shop_sites'][product['shopName']] = site
        if sites:
            try:
                rows = request('currency_rate', CURRENCY_RATES, params={})
                result['currency_rates'] = rows
                for currency in sorted({'CNY', *(SITE_CURRENCIES[site] for site in sites)}):
                    values = [row.get('rate') for row in rows if row['name'] == currency]
                    try:
                        if len(values) != 1 or money(values[0]) <= 0 or (currency == 'CNY' and money(values[0]) != 1):
                            raise ValueError()
                    except ValueError:
                        issue('currency_rate', 'currency_rate_missing_or_invalid', currency=currency)
            except _Issue as error:
                issue('currency_rate', error.code)
            if progress:
                progress('读取销售海外仓费率，保留集团至店铺各层配置。')
            try:
                rows = pages('overseas_rate', OVERSEAS_RATES, {
                    'blocNames':[settings.erp_capture_bloc], 'siteCodes':sorted(sites)})
                if any(row.get('blocName') != settings.erp_capture_bloc or row.get('siteCode') not in sites
                       or type(row.get('authType')) is not int or row['authType'] not in range(1, 6) for row in rows):
                    raise _Issue('overseas_rate_scope_mismatch')
                result['overseas_rates'] = rows
            except _Issue as error:
                issue('overseas_rate', error.code)

        departments = sorted({row['department'] for row in result['products']
                              if isinstance(row.get('department'), str) and row['department'].strip()})
        for department in departments:
            try:
                rows = pages('department', DEPARTMENT_RATES, {'authType':2,
                    'blocName':settings.erp_capture_bloc, 'departmentName':department})
                if any(row.get('authType') != 2 or row.get('departmentName') != department
                       or row.get('blocName') != settings.erp_capture_bloc for row in rows):
                    raise _Issue('department_scope_mismatch')
                result['department_rates'][department] = rows
            except _Issue as error:
                issue('department', error.code, department=department)
        for shop in sorted({row['shopName'] for row in result['products']}):
            try:
                payload = request('public', PUBLIC_RATES, params={'shopName':shop, 'globalPublish':False})
                rows = payload.get('datas')
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise _Issue('public_response_invalid')
                result['public_rates'][shop] = rows
            except _Issue as error:
                issue('public', error.code, shop=shop)
    finally:
        private_request.reset(context)
        if owned:
            client.close()
    result['missing_product_ids'] = sorted(products_requested - {row['productId'] for row in result['products']})
    result['acquisition_complete'] = not result['issues']
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    result['summary'] = {'requested_products':len(requested), 'products':len(result['products']),
        'variants':len(result['variants']), 'departments':len(result['department_rates']), 'shops':len(result['public_rates'])}
    result['artifact_file'] = str((directory / 'result.json').resolve())
    result = _json_values(result)
    write_verified(result['artifact_file'], result)
    return result
