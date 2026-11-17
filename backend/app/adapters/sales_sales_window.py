"""销售网页子SKU七日销量，按ERP真实主SKU归集（2026-11-17用户确认）。

父商品主SKU只用于发现链接，不作为变体归属或销量。以候选子SKU及主SKU
种子链接展开同店关联链接；ERP官方目录只提供身份，不消费仓库销量。
sevenDays未提供准确窗口边界；发现范围不冒充整个ERP主SKU规格目录。
"""
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.erp_sku_catalog import capture_sku_catalog
from app.adapters.normalization import identity_text, sku_text
from app.adapters.sales_auth import ORIGIN, SalesAuth
from app.adapters.sales_capture import PRODUCTS, VARIANTS
from app.adapters.transport_privacy import private_request
from app.domain.engine import BEIJING
from app.services.erp_capture_store import write_verified

BASIS = 'sales_system_web_sku_seven_days'


def capture_sales_window(settings, capture, targets, *, client=None, page_size=100, progress=None):
    if settings.vendor_api_url.rstrip('/') != ORIGIN:
        raise SourceUnavailable('七日销量来源地址未确认')
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError('销量页大小须为1至1000')
    plans, seeds = defaultdict(dict), defaultdict(lambda: defaultdict(set))
    for target in targets:
        shop, shop_id, main = (identity_text(target[k]) for k in ('erp_shop', 'shop_id', 'main_sku'))
        if plans[shop] and shop_id not in plans[shop].values():
            raise ValueError('销量目标的店铺身份冲突')
        plans[shop][main] = shop_id
        if target.get('erp_sku'):
            seeds[shop][main].add(sku_text(target['erp_sku']))
    source_id = 'sales-seven-days-' + uuid4().hex
    directory = Path(settings.runtime_dir) / 'sales-sales-windows' / source_id
    directory.mkdir(mode=0o700, parents=True)
    result = {'schema_version': 'sales-web-sku-seven-days-v2', 'source_id': source_id,
        'erp_capture_id': capture['capture_id'], 'synthetic': bool(capture.get('synthetic', False)),
        'sample_only': bool(capture.get('sample_only', False)), 'queried_at': datetime.now(timezone.utc).isoformat(),
        'basis': BASIS, 'window_start': None, 'window_end': None, 'window_semantics_verified': False,
        'discovery_scope': 'target_skus_and_main_label_links_expanded_by_verified_child_skus',
        'erp_main_catalog_exhaustive': False, 'volumes': [], 'files': [], 'issues': [],
        'contributions': [], 'mixed_links': [], 'catalog_sources': [], 'pricing_ready': False,
        'artifact_file': str((directory / 'result.json').resolve())}
    owned = client is None
    client = client or httpx.Client(timeout=40, trust_env=False, follow_redirects=False,
                                    headers={'Connection': 'close'})
    auth = SalesAuth(settings, client)
    context = private_request.set(True)
    last_request, stopped = None, False
    mapping, mapped_attempts, mapping_refs = {}, set(), {}

    def request(path, body):
        nonlocal last_request, stopped
        if stopped:
            raise SourceUnavailable('销量来源已停止')
        if last_request is not None:
            sleep(max(0, settings.sales_request_interval_seconds - (monotonic() - last_request)))
        token = auth.get_token()
        last_request = monotonic()
        response = client.post(ORIGIN + path, json=body, headers={'Authorization': 'Bearer ' + token},
                               timeout=40, follow_redirects=False)
        observed = datetime.now(timezone.utc)
        file = directory / f'{len(result["files"])+1:05d}.json'
        file.write_bytes(response.content)
        raw = file.read_bytes()
        if raw != response.content:
            raise OSError('销量原件回读不一致')
        ref = f'{source_id}/response/{len(result["files"])+1}'
        result['files'].append({'file': str(file.resolve()), 'sha256': sha256(raw).hexdigest(),
            'bytes': len(raw), 'path': path, 'request': body, 'http_status': response.status_code,
            'received_at': observed.isoformat(), 'ref': ref})
        if response.status_code in (401, 403, 429):
            stopped = True
        response.raise_for_status()
        payload = response.json()
        code = 'code' if path == PRODUCTS else 'resp_code'
        if not isinstance(payload, dict) or type(payload.get(code)) is not int or payload[code] != 0:
            raise ValueError('sales_business_failed')
        return payload, observed, ref

    def resolve(skus):
        needed = set(skus) - mapped_attempts
        if not needed:
            return
        catalog = capture_sku_catalog(settings, needed, erp_capture_id=capture['capture_id'],
            synthetic=result['synthetic'], sample_only=result['sample_only'], client=client, progress=progress)
        mapped_attempts.update(needed)
        result['catalog_sources'].append({k: catalog[k] for k in
            ('source_id', 'artifact_file', 'acquisition_complete', 'missing_skus', 'issues')})
        # 目录采集器只在整页身份/总数/重复校验通过后发布映射。
        mapping.update(catalog['mapping'])
        for row in catalog['records']:
            mapping_refs[row['sku']] = row['ref']

    try:
        for shop, scope in sorted(plans.items()):
            mains = sorted(scope)
            invalid, parents, children, queried_skus = set(), {}, {}, set()
            started = datetime.now(timezone.utc)
            observed_at = None
            shop_id = scope[mains[0]]

            def issue(code, affected, error=None):
                invalid.update(affected)
                entry = {'code': code, 'erp_shop': shop, 'main_skus': sorted(affected)}
                if error is not None:
                    entry['error_kind'] = type(error).__name__
                result['issues'].append(entry)

            def discover(filters, affected):
                nonlocal observed_at, stopped
                page, total, seen_ids, seen_products, found = 1, None, set(), set(), []
                try:
                    while True:
                        if progress:
                            progress(f'读取销售七日销量链接：{shop}，第{page}页。')
                        body = {'page': page, 'limit': page_size, 'shopNameStr': shop, **filters}
                        payload, observed_at, _ = request(PRODUCTS, body)
                        rows, count = payload.get('data'), payload.get('count')
                        if not isinstance(rows, list) or type(count) is not int or count < 0:
                            raise ValueError('sales_pagination_incomplete')
                        if total is None:
                            total = count
                        if count != total or len(rows) != min(page_size, max(0, total-len(seen_ids))):
                            raise ValueError('sales_pagination_incomplete')
                        for row in rows:
                            pid, rid = identity_text(row.get('productId')), identity_text(row.get('id'))
                            if row.get('shopName') != shop or row.get('shopId') != shop_id or rid in seen_ids or pid in seen_products:
                                raise ValueError('sales_scope_invalid')
                            if 'localMainSkuStr' in filters and row.get('localMainSku') not in affected:
                                raise ValueError('sales_scope_invalid')
                            if pid in parents and any(parents[pid].get(k) != row.get(k) for k in ('id', 'shopId', 'localMainSku')):
                                raise ValueError('sales_identity_changed')
                            seen_ids.add(rid); seen_products.add(pid); found.append(row)
                        if len(seen_ids) == total:
                            break
                        page += 1
                    for row in found:
                        parents.setdefault(row['productId'], row)
                    return seen_products
                except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as error:
                    if isinstance(error, SourceUnavailable):
                        stopped = True
                    issue('sales_link_query_incomplete', affected, error)
                    return set()

            for offset in range(0, len(mains), 50):
                batch = mains[offset:offset+50]
                if any(any(c.isspace() for c in text) for text in (shop, *batch)):
                    issue('sales_scope_not_queryable', batch)
                    continue
                discover({'mainSkuType': 1, 'localMainSkuStr': ' '.join(batch)}, batch)

            while not stopped and set(mains) - invalid:
                for pid in sorted(set(parents) - children.keys()):
                    try:
                        payload, observed_at, ref = request(VARIANTS + quote(shop, safe='') + '/' + quote(pid, safe=''),
                                                           {'shopName': shop, 'productId': pid})
                        rows = payload.get('datas')
                        if not isinstance(rows, list) or not rows:
                            raise ValueError('sales_variants_incomplete')
                        seen = set()
                        for row in rows:
                            item = identity_text(row.get('sellerSkuId'))
                            sku_text(row.get('localSellerSku'))
                            if row.get('shopName') != shop or row.get('productId') != pid or row.get('shopId') != shop_id or item in seen:
                                raise ValueError('sales_variant_identity_invalid')
                            seen.add(item)
                        children[pid] = [(row, ref, observed_at.isoformat()) for row in rows]
                    except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as error:
                        if isinstance(error, SourceUnavailable):
                            stopped = True
                        issue('sales_variants_incomplete', mains, error)
                        break
                if stopped or not set(mains) - invalid:
                    break
                discovered_skus = {row['localSellerSku'] for entries in children.values() for row, _, _ in entries}
                seed_skus = {sku for values in seeds[shop].values() for sku in values}
                try:
                    resolve(discovered_skus | seed_skus)
                except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError) as error:
                    issue('sales_main_mapping_unavailable', mains, error)
                    break
                if (discovered_skus | seed_skus) - mapping.keys():
                    issue('sales_main_mapping_unknown', mains)
                    break
                for main in mains:
                    if any(mapping[sku] != main for sku in seeds[shop][main]):
                        issue('sales_target_main_conflict', [main])
                pending = sorted(sku for sku in discovered_skus | seed_skus
                    if mapping[sku] in scope and mapping[sku] not in invalid and sku not in queried_skus)
                if not pending:
                    break
                for offset in range(0, len(pending), 50):
                    batch = pending[offset:offset+50]
                    affected = {mapping[sku] for sku in batch}
                    if any(any(c.isspace() for c in sku) for sku in batch):
                        issue('sales_scope_not_queryable', affected)
                        continue
                    known_products = {pid for pid, entries in children.items()
                        if any(row['localSellerSku'] in batch for row, _, _ in entries)}
                    found_products = discover({'localSellerSkuStr': ' '.join(batch)}, affected)
                    if not known_products <= found_products:
                        issue('sales_child_query_missing_known_links', affected)
                    queried_skus.update(batch)

            quantities, products, main_skus = defaultdict(int), defaultdict(set), defaultdict(set)
            for pid, entries in children.items():
                true_mains = {mapping.get(row['localSellerSku']) for row, _, _ in entries}
                if len(true_mains - {None}) > 1:
                    result['mixed_links'].append({'erp_shop': shop, 'product_id': pid,
                        'parent_main_sku': parents[pid].get('localMainSku'), 'true_main_skus': sorted(true_mains - {None})})
                for row, ref, observed in entries:
                    sku, quantity = row['localSellerSku'], row.get('sevenDays')
                    main = mapping.get(sku)
                    included = main in scope
                    valid = type(quantity) is int and quantity >= 0
                    result['contributions'].append({'erp_shop': shop, 'product_id': pid,
                        'item_id': row['sellerSkuId'], 'sku': sku, 'true_main_sku': main,
                        'parent_main_sku': parents[pid].get('localMainSku'), 'quantity': quantity,
                        'included': included and valid, 'source_ref': ref, 'observed_at': observed,
                        'catalog_ref': mapping_refs.get(sku)})
                    if included:
                        products[main].add(pid); main_skus[main].add(sku)
                        if valid:
                            quantities[main] += quantity
                        else:
                            issue('sales_quantity_unknown', [main])
            if stopped:
                invalid.update(mains)
            if observed_at and started.astimezone(BEIJING).date() != observed_at.astimezone(BEIJING).date():
                issue('sales_pagination_crossed_business_day', mains)
            for main in mains:
                known = main not in invalid and bool(products[main])
                if not products[main] and main not in invalid:
                    issue('sales_target_variants_missing', [main])
                result['volumes'].append({'erp_shop': shop, 'shop_id': scope[main], 'main_sku': main,
                    # 完成关联范围查询不等于穷尽真主SKU规格。保留参考值，
                    # 下游不得把可能漏掉独立混挂规格的低值用于自动调价。
                    'quantity': quantities[main] if known else None, 'complete': False,
                    'discovered_scope_complete': known,
                    'observed_at': observed_at.isoformat() if observed_at else None,
                    'product_ids': sorted(products[main]), 'skus': sorted(main_skus[main])})
    finally:
        private_request.reset(context)
        if owned:
            client.close()
    result['acquisition_complete'] = all(v['discovered_scope_complete'] for v in result['volumes'])
    result['issues'].append({'code': 'sales_main_catalog_not_exhaustive',
        'reason': '参考销量已按真实主SKU拆分；未证明全规格覆盖，不用于自动调价放行'})
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    write_verified(result['artifact_file'], result)
    return result
