"""完整原生明细与订单财务交叉核对，准备SOP允许的同SKU整单金额。

金额记录身份使用真实订单号；原始XLSX物理行引用另行完整保留，不冒充ERP serial。
币种合同是已核对的ERP网页财务RMB金额，不从明细原币或在线站点币猜测。
这里只准备已证明的归属，不补销量、历史、保护或费用模型。
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re

from app.adapters.erp_export_mapping import SUPPORTED_SITES, _product_ids, _text
from app.adapters.erp_pricing import BulkCapture, MappedKey, OrderEvidence
from app.adapters.normalization import sku_text
from app.adapters.sales_contracts import SHOP_SITE_CONTRACT, site_from_shop_name
from app.domain.engine import BEIJING
from app.domain.money import MoneyRatio


VERSION = 'erp-same-sku-order-loss-v1'
CURRENCY_CONTRACT = 'erp-order-finance-rmb-20261111'
NORMAL_ATTRIBUTES = {'正常订单', '小货东南亚真仓订单'}
REASONS = {
    'native_order_details_incomplete':'原生商品明细未取全、为空或物理行引用重复',
    'multi_sku_order':'多SKU订单缺少可靠逐项亏损分摊，不分配整单金额',
    'missing_order_sku':'原生商品明细缺少子SKU',
    'order_finance_incomplete':'候选来源的订单财务不完整或存在重复',
    'invalid_order_margin':'订单修正一次毛利缺失或不是有限金额',
    'conflicting_order_finance':'来源之间的订单金额或身份不一致',
    'order_identity_conflict':'原生明细与订单财务的平台、店铺或订单号不一致',
    'unreviewed_order_attribute':'订单属性不在已核对的正常业务范围',
    'invalid_order_quantity':'原生明细数量缺失、无效或非正数',
    'order_quantity_mismatch':'原生明细总件数未与财务SKU显示数量对齐',
    'ambiguous_online_mapping':'同店同站同仓配匹配到多个在线商品，未选择其中一个',
    'missing_same_warehouse_online_mapping':'缺少可核对的同店同站同仓配在线商品',
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('金额或数量不是来源十进制值')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError('金额或数量无效') from None
    if not result.is_finite():
        raise ValueError('金额或数量非有限值')
    return result


def _stamp(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('来源时点缺少时区')
    return stamp


def _bound(capture, source, schema):
    if (source['schema_version'] != schema or source['erp_capture_id'] != capture['capture_id']
            or source['synthetic'] != capture['synthetic'] or source['sample_only'] != capture['sample_only']):
        raise ValueError('原生明细/销售来源与本轮采集或样本属性不一致')
    captured = _stamp(capture['queried_at'])
    start, end = (_stamp(source[key]) for key in ('queried_at', 'completed_at'))
    day = captured.astimezone(BEIJING).date()
    if start.astimezone(BEIJING).date() != day or end.astimezone(BEIJING).date() != day:
        raise ValueError('原生明细/销售来源跨业务日')
    if start < captured or end < start:
        raise ValueError('原生明细/销售来源观察时序不一致')


def _warehouse_matches(variant, warehouse):
    text = variant.get('overseasStockWarehouseInfo')
    if isinstance(text, str):
        try:
            rows = json.loads(text, parse_float=Decimal)
        except ValueError:
            return False
        if not isinstance(rows, list):
            return False
        matches = []
        for row in rows:
            if not isinstance(row, dict) or row.get('overseasWarehouses') != [warehouse]:
                continue
            try:
                if _number(row.get('quantity')) > 0:
                    matches.append(row)
            except ValueError:
                continue
        if len(matches) == 1:
            return True
    # 缺库存按2026-11-20确认选直发，但仍须默认仓库名精确匹配，不放宽归属。
    try:
        stock = variant.get('overseasStockSum')
        return (stock in (None, '') or _number(stock) == 0) and variant.get('warehousename') == warehouse
    except ValueError:
        return False


def prepare_order_losses(capture, native, sales=None, *, order_headers=None):
    header = BulkCapture.model_validate(capture)
    capture = dict(capture, synthetic=header.synthetic, sample_only=header.sample_only)
    _bound(capture, native, 'erp-order-details-v1')
    if sales is not None:
        _bound(capture, sales, 'sales-source-observations-v1')
    if order_headers is not None:
        _bound(capture, order_headers, 'erp-order-headers-v1')
    finance, memberships, details = defaultdict(list), defaultdict(set), defaultdict(list)
    for group in capture['groups']:
        for order in group['candidate_order_ids']:
            memberships[order].add(group['source'])
        for index, row in enumerate(group['rows']):
            finance[row['orderid']].append((group['source'], index, row['fields']))
    if set(native['requested_order_ids']) != set(memberships):
        raise ValueError('原生明细请求范围与本轮候选不一致')
    for row in native['rows']:
        if row['order_id'] not in memberships:
            raise ValueError('原生明细存在本轮范围外订单')
        details[row['order_id']].append(row)
    known_orders = {order for batch in native['batches'] if batch['status'] in ('complete', 'partial')
                    for order in batch['order_ids']}
    batch_export = sales is not None and sales.get('source_mode') == 'online_sku_batch_export'
    parents, catalog = defaultdict(set), defaultdict(list)
    if sales is not None:
        for index, row in enumerate(sales['products']):
            site = site_from_shop_name(row.get('shopName'))
            if batch_export and row.get('exportSite') != site:
                continue
            parents[row.get('shopName'), row.get('productId'), row.get('bloc')].add((
                site, _text(row.get('localMainSku')),
                _text(row.get('shopId')), index))
        for index, row in enumerate(sales['variants']):
            catalog[row.get('shopName'), row.get('productId'), row.get('localSellerSku')].append((index, row))
    result = {'schema_version':'erp-order-loss-preparation-v1', 'version':VERSION,
        'erp_capture_id':capture['capture_id'], 'synthetic':capture['synthetic'], 'sample_only':capture['sample_only'],
        'observed_at':max(_stamp(source['completed_at']) for source in (native,sales,order_headers) if source is not None).isoformat(),
        'pricing_ready':False, 'orders':[], 'issues':[], 'proofs':{}, 'shop_scopes':{},
        'references':{'native_source':native['source_id'], 'sales_source':sales['source_id'] if sales else None,
                      'currency_contract':CURRENCY_CONTRACT, 'shop_site_contract':SHOP_SITE_CONTRACT,
                      'order_headers':order_headers['source_id'] if order_headers else None}}

    def issue(order, code):
        result['issues'].append({'order_id':order, 'code':code, 'reason':REASONS[code]})

    if order_headers is not None:
        if (len(set(order_headers['requested_order_ids']))!=len(order_headers['requested_order_ids'])
                or set(order_headers['requested_order_ids']) != set(memberships)
                or set(order_headers['orders']) - set(memberships)):
            raise ValueError('订单归属请求与本轮候选不一致')
        for order, header_row in order_headers['orders'].items():
            observations = finance[order]
            try:
                signatures = {(_text(f.get('店铺')), f.get('平台'))
                    for _, _, f in observations}
            except ValueError:
                continue
            if (order not in memberships or len(observations)!=len(memberships[order])
                    or len({source for source,_,_ in observations})!=len(observations)
                    or len(signatures)!=1):
                continue
            shop, platform = next(iter(signatures))
            if (header_row.get('orderid')!=order or header_row.get('shopname')!=shop
                    or not _text(header_row.get('shopid')) or not isinstance(platform,str)
                    or platform.casefold()!='tiktok' or str(header_row.get('platform')).casefold()!='tiktok'
                    or any(f.get('订单编号')!=order for _,_,f in observations)
                    or any(row['fields'].get('店铺')!=shop or row['fields'].get('订单编号')!=order
                        or str(row['fields'].get('平台')).casefold()!='tiktok' for row in details[order])):
                continue
            result['shop_scopes'][order] = {'shop_id':header_row['shopid'],'erp_shop':shop,'ref':header_row['ref']}

    for order in sorted(memberships):
        rows = details[order]
        if order not in known_orders or not rows or len({row['ref'] for row in rows}) != len(rows):
            issue(order, 'native_order_details_incomplete')
            continue
        fields = [row['fields'] for row in rows]
        skus = {_text(row.get('商品SKU列表'), sku_text) for row in fields}
        if None in skus or len(skus) != 1:
            issue(order, 'multi_sku_order' if len(skus) > 1 else 'missing_order_sku')
            continue
        sku = next(iter(skus))
        observations = finance[order]
        if (len(observations) != len(memberships[order])
                or len({source for source, _, _ in observations}) != len(observations)):
            issue(order, 'order_finance_incomplete')
            continue
        try:
            signatures = {(_text(f.get('店铺')), _text(f.get('集团')), f.get('平台'), f.get('商品SKU列表'),
                           f.get('实体仓库名称'), f.get('订单属性'), _product_ids(f.get('item ID')),
                           _number(f.get('修正一次毛利'))) for _, _, f in observations}
        except ValueError:
            issue(order, 'invalid_order_margin')
            continue
        if len(signatures) != 1:
            issue(order, 'conflicting_order_finance')
            continue
        shop, bloc, platform, display, warehouse, attribute, products, amount = next(iter(signatures))
        if (not shop or not bloc or not isinstance(platform, str) or platform.casefold() != 'tiktok'
                or any(f.get('订单编号') != order for _, _, f in observations)
                or any(f.get('订单编号') != order or f.get('店铺') != shop
                       or not isinstance(f.get('平台'), str) or f['平台'].casefold() != 'tiktok' for f in fields)):
            issue(order, 'order_identity_conflict')
            continue
        if attribute not in NORMAL_ATTRIBUTES:
            issue(order, 'unreviewed_order_attribute')
            continue
        try:
            quantities = [_number(f.get('订单数量')) for f in fields]
            if any(value <= 0 for value in quantities):
                raise ValueError()
            quantity = sum((MoneyRatio(value) for value in quantities), MoneyRatio(Decimal(0))).numerator
        except ValueError:
            issue(order, 'invalid_order_quantity')
            continue
        match = re.fullmatch(re.escape(sku) + r'x([1-9][0-9]*)(?:\[[^\[\]]*\])?', display) if isinstance(display, str) else None
        if match is None or Decimal(match[1]) != quantity:
            issue(order, 'order_quantity_mismatch')
            continue
        mappings = {}
        for product in products:
            parent_rows = parents[shop, product, bloc]
            if len(parent_rows) != 1:
                continue
            site, main_sku, shop_id, parent_index = next(iter(parent_rows))
            scope = result['shop_scopes'].get(order)
            if scope is not None and scope['shop_id'] != shop_id:
                continue
            if site not in SUPPORTED_SITES or main_sku is None or shop_id is None:
                continue
            for index, variant in catalog[shop, product, sku]:
                if (variant.get('shopId') != shop_id or (variant.get('localMainSku') is not None
                        and variant.get('localMainSku') != main_sku)):
                    continue
                # 批量文件先确认子商品身份以查询官方保护，实际算价前另核对动态仓配。
                if not warehouse or (not batch_export and not _warehouse_matches(variant, warehouse)):
                    continue
                try:
                    key = MappedKey(platform='TikTok', shop_id=variant.get('shopId'), site=site,
                        product_id=product, item_id=variant.get('sellerSkuId'), sku=variant.get('sellerSku'), warehouse=warehouse)
                except ValueError:
                    continue
                identity = (key.identity(), main_sku)
                mappings.setdefault(identity, {'erp_shop':shop, 'key':key.model_dump(mode='json'),
                    'main_sku':main_sku, 'ref':variant['export_ref'] if batch_export else
                    f'{sales["source_id"]}/products/{parent_index}/variants/{index}'})
        mapping = next(iter(mappings.values())) if len(mappings) == 1 else None
        if mapping is None and amount < 0:
            issue(order, 'ambiguous_online_mapping' if len(mappings) > 1 else 'missing_same_warehouse_online_mapping')
        record_ref = f'{native["source_id"]}/same-sku-order/{order}'
        entry = OrderEvidence.model_validate({'order_id':order, 'details_complete':True, 'lines':[{
            'detail_id':order, 'detail_ref':record_ref, 'mapping':mapping,
            'loss':{'quantity':format(quantity, 'f'), 'amount':format(amount, 'f'), 'currency':'CNY',
                'basis':'order', 'allocation_reliable':True, 'fee_scope':'erp-corrected-first-margin-v1',
                'abnormal':False, 'ref':f'{capture["capture_id"]}/order-finance/{order}'}}]})
        result['orders'].append(entry.model_dump(mode='json'))
        result['proofs'][order] = {'record_kind':'same_sku_order', 'order_record_id':order,
            'native_refs':[row['ref'] for row in rows], 'quantity':format(quantity, 'f'), 'erp_sku':sku,
            'finance_refs':[{'source':source, 'row_index':index} for source, index, _ in observations],
            'currency':'CNY', 'currency_contract':CURRENCY_CONTRACT}
    result['summary'] = {'candidate_orders':len(memberships), 'prepared_order_losses':len(result['orders']),
        'mapped_orders':sum(order['lines'][0]['mapping'] is not None for order in result['orders']),
        'issues':len(result['issues'])}
    return result
