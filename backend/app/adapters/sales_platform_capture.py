"""逐店铺产品读取平台当前身份/可售/价格证据；未知字段保留未知，不映射默认放行。"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from urllib.parse import quote
import json
import httpx
from app.adapters.base import SourceUnavailable
from app.adapters.normalization import identity_text
from app.adapters.sales_auth import SalesAuth, ORIGIN
from app.adapters.sales_capture import _json_values
from app.adapters.transport_privacy import private_request
from app.services.erp_capture_store import write_verified

PATH = '/Api/api-tiktok-sales/online/getEditOnlineDataByShopNameAndProductId'

ACTIVITY_PATH = '/Api/api-tiktok-sales/online/findByPage'
BATCH_SIZE = 100
# 真实线上活动查询用 8 个产品完成两侧取数，12 个产品出现读超时。分页仍按原合同核对。
ACTIVITY_PRODUCTS_PER_BATCH = 8


def _archive(directory, result, response, filename, path, request):
    file = directory / filename
    raw = response.content
    file.write_bytes(raw)
    received_at = datetime.now(timezone.utc).isoformat()
    result['files'].append({'file':str(file.resolve()), 'bytes':len(raw), 'sha256':sha256(raw).hexdigest(),
        'path':path, 'request':request, 'http_status':response.status_code, 'received_at':received_at})
    response.raise_for_status()
    return received_at


def _activity_pages(client, auth, directory, result, batch_number, active, products, bloc):
    page = 1
    while True:
        body = {'page': page, 'limit': BATCH_SIZE, 'blocNames': [bloc],
                'productIdStr': ' '.join(products), 'isPromotion': '0' if active == 0 else 1}
        response = client.post(ORIGIN + ACTIVITY_PATH, json=body, headers=auth.collection_headers(),
                               timeout=60, follow_redirects=False)
        auth.observe_collection_response(response)
        received = _archive(directory, result, response,
                            f'activity-{batch_number:05d}-{active}-{page:04d}.json', ACTIVITY_PATH, body)
        value = response.json(parse_float=Decimal)
        if not isinstance(value, dict) or type(value.get('code')) is not int or value['code'] != 0:
            raise ValueError('company_activity_business_failed')
        count, rows = value.get('count'), value.get('data')
        if type(count) is not int or count < 0 or not isinstance(rows, list):
            raise ValueError('company_activity_coverage_unverified')
        accepted = []
        for row in rows:
            if isinstance(row, dict) and row.get('bloc') == bloc and row.get('productId') in products:
                try:
                    shop = identity_text(row.get('shopName'))
                except ValueError:
                    continue
                accepted.append(((shop, row['productId']), row))
        if len(accepted) != len(rows):
            result['issues'].append({'code':'company_activity_scope_mismatch', 'batch':batch_number,
                                    'active':active, 'page':page, 'excluded_rows':len(rows) - len(accepted)})
        yield received, accepted
        if len(rows) < BATCH_SIZE or page * BATCH_SIZE >= count:
            break
        page += 1


def _activity_product_batches(shops_by_product, interval):
    # 每批按需查一侧或两侧，每店铺产品再读两次。按节流时间估算批长，
    # 给现态快照和可选参考留出余量；实际耗时和页数仍以观测时间为准。
    max_links = max(1, int(240 / (2 * interval)) - 1) if interval > 0 else None
    batch, links = [], 0
    for product, shops in shops_by_product.items():
        if batch and (
            len(batch) == ACTIVITY_PRODUCTS_PER_BATCH or (max_links is not None and links + len(shops) > max_links)
        ):
            yield batch
            batch, links = [], 0
        batch.append(product)
        links += len(shops)
    if batch:
        yield batch


def _confirmed_activity(settings, plans, client, auth, directory, result, activity_hints):
    shops_by_product = defaultdict(list)
    for shop, product in sorted(plans, key=lambda identity: (identity[1], identity[0])):
        if any(char.isspace() for char in product):
            result['issues'].append({'code':'company_activity_invalid_product_id',
                'erp_shop':shop, 'product_id':product, 'item_ids':sorted(plans[shop, product])})
            continue
        shops_by_product[product].append(shop)
    batches = _activity_product_batches(shops_by_product, settings.sales_request_interval_seconds)
    for batch_number, batch in enumerate(batches, 1):
        matches = defaultdict(list)
        observed_at = None
        for active in (0, 1):
            products = [product for product in batch if any(
                activity_hints.get((shop, product)) in (active, None) for shop in shops_by_product[product]
            )]
            if not products:
                continue
            try:
                for received, rows in _activity_pages(client, auth, directory, result, batch_number,
                                                      active, products, settings.erp_capture_bloc):
                    observed_at = observed_at or received
                    for identity, row in rows:
                        matches[identity].append((active, row))
            except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError) as error:
                issue = {'code':'company_activity_batch_unverified', 'product_ids':products,
                         'active':active, 'error_kind':type(error).__name__}
                if isinstance(error, ValueError) and str(error).startswith('company_activity_'):
                    issue['reason'] = str(error)
                if isinstance(error, httpx.HTTPStatusError):
                    issue['http_status'] = error.response.status_code
                result['issues'].append(issue)
                if isinstance(error, SourceUnavailable) or auth.stopped_http_status is not None:
                    return
        for product in batch:
            for shop in shops_by_product[product]:
                rows = matches.get((shop, product), [])
                expected = activity_hints.get((shop, product))
                if len(rows) != 1 or (expected is not None and bool(rows[0][0]) != expected):
                    result['issues'].append({'code':'company_activity_state_conflicting_or_missing',
                        'erp_shop':shop, 'product_id':product, 'item_ids':sorted(plans[shop, product])})
                    continue
                active, parent = rows[0]
                yield (shop, product), active, parent, observed_at


def capture_platform_controls(settings,capture,targets,*,client=None,auth=None,progress=None,activity_hints=None):
    if settings.vendor_api_url.rstrip('/') != ORIGIN:
        raise SourceUnavailable('平台现态来源地址未确认')
    plans=defaultdict(set)
    for target in targets:
        shop,product,item=(identity_text(target[key]) for key in ('erp_shop','product_id','item_id'))
        plans[shop,product].add(item)
    source_id='sales-platform-controls-'+uuid4().hex
    directory=Path(settings.runtime_dir)/'sales-platform-controls'/source_id
    directory.mkdir(mode=0o700,parents=True)
    result={'schema_version':'sales-platform-controls-v1','source_id':source_id,
        'erp_capture_id':capture['capture_id'],'synthetic':bool(capture.get('synthetic',False)),
        'sample_only':bool(capture.get('sample_only',False)),'queried_at':datetime.now(timezone.utc).isoformat(),
        'requested':[{'erp_shop':shop,'product_id':product,'item_ids':sorted(items)}
            for (shop,product),items in sorted(plans.items())],
        'records':[],'files':[],'issues':[],'pricing_ready':False,
        'artifact_file':str((directory/'result.json').resolve())}
    owned=client is None
    client=client or httpx.Client(timeout=60,trust_env=False,follow_redirects=False)
    auth=auth or SalesAuth(settings,client)
    context=private_request.set(True)
    try:
        confirmed = _confirmed_activity(settings, plans, client, auth, directory, result, activity_hints or {})
        for number, ((shop, product), active, parent, activity_observed) in enumerate(confirmed, 1):
            items = plans[shop, product]
            if progress:progress(f'读取平台当前商品身份与可售状态：第{number}/{len(plans)}个产品。')
            try:
                params = {'shopName':shop,'productId':product}
                response = client.get(ORIGIN+PATH,params=params,headers=auth.collection_headers(),
                                      timeout=60,follow_redirects=False)
                auth.observe_collection_response(response)
                received = _archive(directory, result, response, f'{number:05d}.json', PATH, params)
                value=response.json(parse_float=Decimal)
                if not isinstance(value,dict) or type(value.get('resp_code')) is not int or value['resp_code']!=0:
                    raise ValueError('platform_business_failed')
                data=value.get('datas')
                if not isinstance(data,dict) or data.get('id')!=product or not isinstance(data.get('skus'),list):
                    raise ValueError('platform_product_scope_mismatch')
                skus=data['skus']
                ids=[identity_text(sku['id']) for sku in skus if isinstance(sku,dict)]
                if len(ids)!=len(skus) or len(ids)!=len(set(ids)) or not items<=set(ids):
                    raise ValueError('platform_sku_scope_mismatch')
                # 原件保留，规范来源仅投影本流程所需业务字段，缺字段不补默认值。
                projected={key:data.get(key) for key in ('id','status','product_status','is_not_for_sale','update_time')}
                projected['skus']=[{key:sku.get(key) for key in ('id','seller_sku','price','status_info','inventory')}
                    for sku in skus]
                variant_path='/Api/api-tiktok-sales/online/findProductVariants/'+quote(shop,safe='')+'/'+quote(product,safe='')
                variant_body={'shopName':shop,'productId':product}
                variant_response=client.post(ORIGIN+variant_path,json=variant_body,
                    headers=auth.collection_headers(),timeout=60,follow_redirects=False)
                auth.observe_collection_response(variant_response)
                variants_observed = _archive(directory, result, variant_response, f'{number:05d}-variants.json',
                                             variant_path, variant_body)
                value=variant_response.json(parse_float=Decimal)
                if not isinstance(value,dict) or type(value.get('resp_code')) is not int or value['resp_code']!=0:
                    raise ValueError('current_variant_business_failed')
                variants=value.get('datas')
                if (not isinstance(variants,list) or any(not isinstance(row,dict) or row.get('shopName')!=shop
                        or row.get('productId')!=product for row in variants)):
                    raise ValueError('current_variant_scope_mismatch')
                ids=[identity_text(row.get('sellerSkuId')) for row in variants]
                if len(ids)!=len(set(ids)) or not items<=set(ids):
                    raise ValueError('current_variant_identity_ambiguous')
                result['records'].append({'platform_price_observed_at':received,'variants_observed_at':variants_observed,'company_activity_locked':bool(active),'company_parent':_json_values(parent),
                    'current_variants':_json_values(variants),
                    'erp_shop':shop,'product_id':product,'item_ids':sorted(items),
                    # 活动证据早于详情；核心时效从最早观测算起，不能用完成时间刷新它。
                    'observed_at':activity_observed,'data':_json_values(projected),'ref':f'{source_id}/{number}'})
            except (SourceUnavailable,httpx.HTTPError,ValueError,TypeError,KeyError) as error:
                issue = {'code':'platform_current_state_unverified','erp_shop':shop,
                         'product_id':product,'item_ids':sorted(items),'error_kind':type(error).__name__}
                if isinstance(error, httpx.HTTPStatusError):
                    issue['http_status'] = error.response.status_code
                result['issues'].append(issue)
                if isinstance(error, SourceUnavailable) or auth.stopped_http_status is not None:
                    break
    finally:
        private_request.reset(context)
        if owned:client.close()
    if plans and auth.stopped_http_status is not None:
        result['stopped_http_status'] = auth.stopped_http_status
    result['acquisition_complete']=len(result['records'])==len(plans) and not result['issues']
    result['completed_at']=datetime.now(timezone.utc).isoformat()
    write_verified(result['artifact_file'],result)
    return result
