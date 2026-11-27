"""从集团归档的财务观察生成部门档案。原始请求、分页和 Excel 留在父归档。"""
from collections import Counter, defaultdict

from app.adapters.erp_bulk_capture import finance_batch_scope


def _project_group(group, belongs):
    rows = [r for r in group['rows'] if r['orderid'] in belongs]
    returned = {r['orderid'] for r in rows}
    # 另一来源返回同单的财务只能确认部门，不能补作本来源的财务观察。
    candidates = [order for order in group['candidate_order_ids'] if order in returned]
    losses = [r for r in group.get('loss_rows', ()) if r['订单编号'] in returned]
    discrepancies = [r for r in group.get('status_discrepancies', ()) if r['orderid'] in returned]
    batches = []
    offset = 0
    for batch in group.get('batches', ()):
        if batch['status'] not in ('complete', 'partial'):
            continue
        end = offset + batch['scope_check']['downloaded_rows']
        actual = [r['orderid'] for r in group['rows'][offset:end] if r['orderid'] in belongs]
        offset = end
        ids = [order for order in batch['order_ids'] if order in returned]
        if not ids:
            continue
        scope = finance_batch_scope(ids, actual)
        view = {k: batch[k] for k in ('task_id', 'task_name', 'polls', 'downloaded_at') if k in batch}
        # order_ids/scope_check 描述部门子集；上游状态不改写，实际请求按父批号回查。
        view.update(number=len(batches) + 1, parent_number=batch['number'], status=batch['status'],
                    order_ids=ids, scope_check=scope)
        batches.append(view)
    result = {k: group[k] for k in ('source', 'kind', 'status', 'filters', 'endpoint', 'stage', 'error',
                                     'error_kind', 'list_complete', 'list_error', 'failed_pages') if k in group}
    result.update(rows=rows, loss_rows=losses, batches=batches, candidate_order_ids=candidates,
                  selected_orders=len(candidates), fetched_rows=len(rows), files=[],
                  total=len(candidates) if group['list_complete'] else None,
                  missing_order_ids=[], status_discrepancies=discrepancies)
    if 'blocked_shops' in group:
        result['blocked_shops'] = {order: shop for order, shop in group['blocked_shops'].items() if order in returned}
    return result


def department_captures(parent, departments):
    """返回 {部门: 待 save_capture 的归档}, {未分配订单号: 原因}，不读写文件。"""
    names = {d.casefold(): d for d in departments}
    labels = defaultdict(set)
    sources = defaultdict(list)
    sample_orders = set()
    for group in parent['groups']:
        for order in group['candidate_order_ids']:
            sources[order].append(group['source'])
        for row in group['rows']:
            fields = row['fields']
            logistics = fields.get('自选物流')
            if isinstance(logistics, str) and logistics.rsplit('_', 1)[-1].strip() == '来赞宝':
                sample_orders.add(row['orderid'])
            department = fields.get('部门')
            labels[row['orderid']].add(department.casefold() if isinstance(department, str) else None)

    assigned = defaultdict(set)
    unassigned = {}
    for order in sources:
        if order in sample_orders:
            continue
        values = labels[order]
        if not values:
            unassigned[order] = 'missing_order_finance'
        elif len(values) > 1:
            unassigned[order] = 'department_conflict'
        elif (name := names.get(next(iter(values)))) is None:
            unassigned[order] = 'unknown_department'
        else:
            assigned[name].add(order)

    captures = {}
    metadata = {k: parent[k] for k in ('source_version', 'collection_policy_version', 'source_kind', 'synthetic',
                                       'sample_only', 'acquisition_mode', 'queried_at', 'rate_limit_observed',
                                       'finance_calculation', 'not_read_groups', 'error', 'error_kind') if k in parent}
    for name in names.values():
        orders = assigned[name]
        groups = [_project_group(g, orders) for g in parent['groups']]
        details = [r for r in parent.get('details', ()) if r.get('orderid') in orders]
        status_counts = Counter(r['fields'].get('订单状态') or '未知/空值' for g in groups for r in g['rows'])
        captures[name] = dict(metadata, department=name, department_parent_capture_id=parent['capture_id'],
                              observed_sources={order: sources[order] for order in sources if order in orders},
                              selection_complete=parent.get('selection_complete', parent['capture_complete']),
                              capture_complete=parent['capture_complete'],
                              pricing_ready=False, groups=groups, details=details, summary={
                                  'selected_orders': sum(g['selected_orders'] for g in groups), 'orders_not_selected': 0,
                                  'list_rows': sum(status_counts.values()), 'unique_orders': len(orders),
                                  'loss_rows': sum(len(g['loss_rows']) for g in groups), 'detail_rows': len(details),
                                  'status_discrepancies': sum(len(g['status_discrepancies']) for g in groups),
                                  'missing_orders': 0, 'order_status_counts': dict(status_counts),
                              })
    return captures, unassigned
