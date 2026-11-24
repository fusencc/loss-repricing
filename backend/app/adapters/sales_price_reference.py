"""已有销售列表/子规格合同提供的可选 Top3 价格参考，故障不进入定价 issues。

合同依据：2026-11-19/20 真实只读样本。父商品七天销量仅用于发现有销量链接，
排名消费子规格 sevenDays；priceIncludeVat 为站点币/件，discount 为减免百分数。
不读取费用或活动接口。查询预算/分页不完整只省略参考，不扫描全部店铺或使用旧文件。
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.normalization import money, source_time
from app.adapters.sales_auth import COLLECTION_STOP_STATUSES, ORIGIN, SalesAuth
from app.adapters.transport_privacy import private_request
from app.domain.engine import SITE_CURRENCIES
from app.domain.price_reference import PriceReferenceSample, SkuPriceReference, calculate_price_reference
from app.domain.types import VariantKey
from app.observability import event
from app.services.erp_capture_store import write_verified

SCHEMA = 'sales-sku-price-reference-v1'
PRODUCTS = '/Api/api-tiktok-sales/online/findByPage'
VARIANTS = '/Api/api-tiktok-sales/online/findProductVariants/'


def _warehouse_scope(variant):
    stock = money(variant.get('overseasStockSum'))
    if stock == 0:
        name = variant.get('warehousename')
        if not isinstance(name, str) or not name.strip():
            raise ValueError('reference_warehouse_unknown')
        return 'direct', name
    if stock < 0:
        raise ValueError('reference_warehouse_unknown')
    buckets = json.loads(variant['overseasStockWarehouseInfo'], parse_float=Decimal)
    positive = [b for b in buckets if money(b['quantity']) > 0]
    if len(positive) != 1 or len(positive[0]['overseasWarehouses']) != 1:
        raise ValueError('reference_warehouse_unknown')
    return 'overseas', positive[0]['overseasWarehouses'][0]


def _sample(variant):
    retail = money(variant.get('priceIncludeVat'))
    discount = variant.get('discount')
    if discount in (None, ''):
        # 无折扣须有同一响应的现价相等证据；空值本身不等于已确认0折扣。
        if (variant.get('promotionType') not in (None, '')
                or variant.get('promotionId') not in (None, '')
                or money(variant.get('marketPrice')) != retail):
            raise ValueError('reference_discount_unknown')
        discount = '0'
    return PriceReferenceSample(shop_name=variant['shopName'], product_id=variant['productId'],
        item_id=variant['sellerSkuId'], sales_7d=variant['sevenDays'],
        tax_inclusive_price=retail, discount_percent=money(discount))


def capture_sku_price_references(settings, capture, targets, *, platform, client=None, auth=None,
                                 progress=None, request_budget=60):
    """可选来源边界：即使认证、响应或归档失败，也不抛给核心定价编排。"""
    now = lambda: datetime.now(timezone.utc).isoformat()
    sid = 'sku-price-reference-' + uuid4().hex
    result = dict(schema_version=SCHEMA, source_id=sid, erp_capture_id=capture['capture_id'],
        synthetic=bool(capture.get('synthetic', False)), sample_only=bool(capture.get('sample_only', False)),
        queried_at=now(), completed_at=None, records={}, files=[], issues=[])
    directory = Path(settings.runtime_dir) / 'sku-price-references' / sid
    result['artifact_file'] = str((directory / 'result.json').resolve())
    owned = client is None
    client = client or httpx.Client(timeout=20, trust_env=False, follow_redirects=False)
    context = private_request.set(True)
    try:
        directory.mkdir(mode=0o700, parents=True)
        if settings.vendor_api_url.rstrip('/') != ORIGIN:
            raise ValueError('reference_origin_unconfirmed')
        _capture(settings, targets, platform, client, auth or SalesAuth(settings, client), result, directory,
                 request_budget=request_budget, progress=progress)
        result['completed_at'] = now()
        write_verified(result['artifact_file'], result)
    except Exception as error:
        # 可选来源的错误可追踪，但不会制造待处理、补默认价或遮蔽其他有效候选。
        result['issues'].append({'code': 'price_reference_unavailable', 'error_kind': type(error).__name__})
        result['records'] = {}
        result['completed_at'] = now()
        event('dependency.failed', exception=error, source='sales', synthetic=result['synthetic'],
              stage='collect', error_code='optional_price_reference_unavailable')
    finally:
        private_request.reset(context)
        if owned:
            client.close()
    return result


def _capture(settings, targets, platform, client, auth, result, directory, *, request_budget, progress):
    controls = {}
    observations = {}
    variant_cache = {}
    for record in (platform or {}).get('records', []):
        identity = record['erp_shop'], record['product_id']
        controls[identity] = record.get('current_variants', [])
        observations[identity] = record.get('observed_at')
        variant_cache[identity] = record.get('current_variants', [])
    started = monotonic()
    now = datetime.now(timezone.utc)
    budget = 30
    groups = defaultdict(list)
    for target in targets:
        key = VariantKey.model_validate(target['key'])
        current = [v for v in controls.get((target['erp_shop'], key.product_id), [])
                   if v.get('sellerSkuId') == key.item_id and v.get('localSellerSku') == target['erp_sku']]
        if len(current) != 1:
            continue
        try:
            age = (now - source_time(observations[target['erp_shop'], key.product_id])).total_seconds()
            # 本来仍有效的其他目标也不能因等待可选参考跨过300秒。
            if 0 <= age < 300:
                budget = min(budget, 300 - age)
            # 只为至少还有60秒时效余量的目标发起可选参考。
            if age < 0 or age >= 240:
                continue
            scope = _warehouse_scope(current[0])
        except (ValueError, KeyError, TypeError, ArithmeticError):
            continue
        groups[target['erp_sku'], key.site].append((target, key, scope))
    if not groups:
        return
    deadline = started + budget
    used = 0

    def remaining_time():
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError('optional_reference_time_budget')
        return remaining

    def read(path, body):
        nonlocal used
        if used >= request_budget:
            raise ValueError('reference_query_budget_exhausted')
        # 登录续期、跨步骤节流、业务请求共用可选参考的时间预算。
        headers = auth.collection_headers(remaining_time=remaining_time)
        used += 1
        # 各I/O阶段共享剩余预算的份额；流式读取同时检查总时限及响应大小。
        timeout = min(5, remaining_time() / 4)
        with client.stream('POST', ORIGIN + path, json=body,
                headers=headers,
                timeout=httpx.Timeout(timeout, connect=min(3, timeout))) as response:
            if response.status_code == 429:
                auth.observe_collection_response(response)
            remaining_time()
            content = bytearray()
            for chunk in response.iter_bytes():
                remaining_time()
                content.extend(chunk)
                if len(content) > 5 * 1024 * 1024:
                    raise ValueError('reference_response_too_large')
            raw = bytes(content)
        file = directory / f'{used:04d}.json'
        file.write_bytes(raw)
        result['files'].append({'path': path, 'request': body, 'http_status': response.status_code,
            'file': str(file.resolve()), 'sha256': sha256(raw).hexdigest(),
            'observed_at': datetime.now(timezone.utc).isoformat()})
        response.raise_for_status()
        value = json.loads(raw, parse_float=Decimal)
        code = 'code' if path == PRODUCTS else 'resp_code'
        if not isinstance(value, dict) or value.get(code) != 0:
            raise ValueError('reference_business_failed')
        return value

    def parents_for(sku, site):
        rows, seen, total, previous = [], set(), None, None
        for page in range(1, 4):
            value = read(PRODUCTS, {'page': page, 'limit': 100,
                'localSellerSkuStr': sku, 'site': site, 'productStatus': 4,
                'blocNames': [settings.erp_capture_bloc], 'field': 'sevenDaysCount', 'order': 'DESC'})
            batch, count = value.get('data'), value.get('count')
            if (not isinstance(batch, list) or type(count) is not int or count < 0
                    or (total is not None and count != total)
                    or len(batch) != min(100, max(0, count - len(seen)))):
                raise ValueError('reference_pagination_incomplete')
            total = count
            for row in batch:
                identity = row['shopName'], row['productId']
                quantity = row.get('sevenDaysCount')
                if (identity in seen or not row['shopName'].endswith(site) or row.get('productStatus') != 4
                        or row.get('bloc') != settings.erp_capture_bloc
                        or type(quantity) is not int or quantity < 0
                        or (previous is not None and quantity > previous)):
                    raise ValueError('reference_parent_scope_or_sort_invalid')
                seen.add(identity)
                previous = quantity
                if quantity > 0:
                    rows.append(row)
            # 父商品汇总仅用于发现候选；滚动销量的精确窗口边界未由接口给出。
            if previous == 0 or len(seen) == total:
                return rows
        raise ValueError('reference_pagination_limit')

    for (sku, site), definitions in groups.items():
        try:
            if progress:
                progress('查询同 SKU 可选营销参考价。')
            parents = [(parent['shopName'], parent['productId']) for parent in parents_for(sku, site)]
            parent_links = set(parents)
            # 父商品搜索只提供候选上界。排除自身链接后不足三个时，无需请求变体。
            definitions = [(target, key, scope) for target, key, scope in definitions
                           if len(parent_links - {(target['erp_shop'], key.product_id)}) >= 3]
            if not definitions:
                continue
            own_links = {(target['erp_shop'], key.product_id) for target, key, _ in definitions}
            variants = []
            for identity in parents:
                if own_links == {identity}:
                    continue
                if identity not in variant_cache:
                    path = VARIANTS + '/'.join(quote(x, safe='') for x in identity)
                    value = read(path, {'shopName': identity[0], 'productId': identity[1]})
                    if not isinstance(value.get('datas'), list):
                        raise ValueError('reference_variants_invalid')
                    variant_cache[identity] = value['datas']
                for variant in variant_cache[identity]:
                    if (variant.get('shopName'), variant.get('productId')) != identity:
                        raise ValueError('reference_variant_scope_invalid')
                    variants.append(variant)
            for target, key, scope in definitions:
                try:
                    samples = []
                    for variant in variants:
                        if (variant.get('localSellerSku') != target['erp_sku']
                                or variant.get('currency') != SITE_CURRENCIES[site]
                                or (variant.get('shopName'), variant.get('productId'))
                                == (target['erp_shop'], key.product_id)):
                            continue
                        quantity = variant.get('sevenDays')
                        if type(quantity) is not int or quantity < 0:
                            raise ValueError('reference_sales_unknown')
                        if quantity == 0 or _warehouse_scope(variant) != scope:
                            continue
                        samples.append(_sample(variant))
                    ref = calculate_price_reference(samples, sku=target['erp_sku'],
                        currency=SITE_CURRENCIES[site], observed_at=datetime.now(timezone.utc),
                        source_ref=result['source_id'])
                    if ref is not None:
                        result['records'][key.identity()] = ref.model_dump(mode='json')
                except (ValueError, KeyError, TypeError, ArithmeticError) as error:
                    result['issues'].append({'code': 'price_reference_missing',
                        'target': key.identity(), 'error_kind': type(error).__name__})
        except (SourceUnavailable, httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            issue = {'code': 'price_reference_scope_unavailable', 'error_kind': type(error).__name__}
            status = (error.response.status_code if isinstance(error, httpx.HTTPStatusError)
                      else auth.stopped_http_status)
            if status is not None:
                issue['http_status'] = status
            result['issues'].append(issue)
            if (status in COLLECTION_STOP_STATUSES or isinstance(error, (SourceUnavailable, TimeoutError))
                    or used >= request_budget):
                break


def price_reference_for(source, capture, key, *, sku, currency):
    """可选绑定；错误/缺失的参考原件不能成为在线准备失败。"""
    if not source:
        return None
    try:
        if (source['schema_version'] != SCHEMA or source['erp_capture_id'] != capture['capture_id']
                or source['synthetic'] != bool(capture.get('synthetic', False))
                or source['sample_only'] != bool(capture.get('sample_only', False))):
            return None
        ref = SkuPriceReference.model_validate(source['records'].get(key.identity()))
        return ref if ref.sku == sku and ref.currency == currency else None
    except (ValueError, KeyError, TypeError):
        return None
