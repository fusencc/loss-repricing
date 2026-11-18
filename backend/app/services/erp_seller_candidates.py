"""Summarize departmental candidates without changing the pricing input."""
from collections import defaultdict


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def summarize_seller_candidates(capture, headers):
    groups = capture['groups']
    shop_ids = defaultdict(set)
    # The ERP list supplies stable shop IDs. The two XLSX sources supply shop names.
    shop_records = list(headers['orders'].values())
    for group in groups:
        shop_records.extend(group.get('blocked_shops', {}).values())
    for row in shop_records:
        name, shop_id = _text(row.get('shopname')), _text(row.get('shopid'))
        if name and shop_id:
            shop_ids[name].add(shop_id)

    def resolve_shop(name):
        ids = shop_ids.get(_text(name), ())
        return next(iter(ids)) if len(ids) == 1 else None

    sources_by_order = defaultdict(list)
    sellers_by_order = defaultdict(set)
    unverified_shops = {}
    for group in groups:
        source = group['source']
        finance = defaultdict(list)
        for row in group['rows']:
            fields = row['fields']
            finance[row['orderid']].append(fields)
            shop_id = resolve_shop(fields.get('店铺'))
            seller = _text(fields.get('销售员'))
            if shop_id and seller:
                sellers_by_order[shop_id, row['orderid']].add(seller)
        losses = defaultdict(list)
        for row in group.get('loss_rows', []):
            losses[row['订单编号']].append(row)
        blocked = group.get('blocked_shops', {})
        for order_id in group['candidate_order_ids']:
            if source.startswith('loss_'):
                observations = [
                    (resolve_shop(row.get('店铺')), _text(row.get('销售员')))
                    for row in losses[order_id]
                ]
            else:
                observation = blocked.get(order_id, {})
                shop_id = _text(observation.get('shopid'))
                shop_name = _text(observation.get('shopname'))
                # Without the list shop ID, require the list name or corroborating order
                # header and same-source financial row before associating the observation.
                if not shop_id:
                    shop_id = resolve_shop(shop_name)
                if not shop_id and not shop_name and len(finance[order_id]) == 1:
                    header = headers['orders'].get(order_id, {})
                    header_name = _text(header.get('shopname'))
                    if header_name and header_name == _text(finance[order_id][0].get('店铺')):
                        shop_id = resolve_shop(header_name)
                observations = [(shop_id, None)]
            for shop_id, seller in observations or [(None, None)]:
                if shop_id is None:
                    unverified_shops[order_id, source] = {
                        'order_id': order_id, 'sources': [source], 'reason': 'shop_unverified',
                    }
                    continue
                key = shop_id, order_id
                if source not in sources_by_order[key]:
                    sources_by_order[key].append(source)
                if seller:
                    sellers_by_order[key].add(seller)

    sellers = defaultdict(list)
    unresolved = list(unverified_shops.values())
    for (shop_id, order_id), sources in sources_by_order.items():
        candidate = {'shop_id': shop_id, 'order_id': order_id, 'sources': sources}
        owners = sellers_by_order[shop_id, order_id]
        if len(owners) == 1:
            sellers[next(iter(owners))].append(candidate)
        else:
            reason = 'salesperson_conflict' if owners else 'salesperson_unverified'
            unresolved.append({**candidate, 'reason': reason})
    return {'sellers': dict(sellers), 'unresolved': unresolved}
