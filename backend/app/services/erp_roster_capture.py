"""从部门采集归档派生按人算价所需的订单和来源，不再次请求 ERP。"""
from collections import defaultdict
import json
from pathlib import Path
from uuid import uuid4

from app.adapters.erp_bulk_capture import finance_batch_scope
from app.services.erp_capture_store import save_capture


def _seller_group(group, name):
    ids = [order for order in group['candidate_order_ids'] if group['order_owners'].get(order) == name]
    selected = set(ids)
    rows_by_order = defaultdict(list)
    for row in group['rows']:
        if row['orderid'] in selected:
            rows_by_order[row['orderid']].append(row)
    batches, rows, missing = [], [], []
    submitted = set()
    duplicates = False
    for batch in group['batches']:
        orders = [order for order in batch['order_ids'] if order in selected]
        if not orders:
            continue
        projected = {key: batch[key] for key in ('status', 'polls', 'downloaded_at', 'task_id', 'task_name') if key in batch}
        projected.update(number=len(batches) + 1, order_ids=orders)
        submitted.update(orders)
        if batch['status'] in ('complete', 'partial'):
            observed = [row for order in orders for row in rows_by_order[order]]
            scope = finance_batch_scope(orders, [row['orderid'] for row in observed])
            missing.extend(scope['missing'])
            duplicates |= bool(scope['duplicate_rows'])
            projected.update(scope_check=scope, status='partial' if scope['missing'] or scope['duplicate_rows'] else 'complete')
            projected.pop('error', None)
            rows.extend(observed)
        batches.append(projected)
    incomplete = not group['list_complete'] or submitted != selected or any(
        batch['status'] not in ('complete', 'partial') for batch in batches
    )
    stage = ('partial' if incomplete else 'finance_conflict' if duplicates else
             'complete_with_missing' if missing else 'complete')
    return {
        'source': group['source'], 'kind': group['kind'], 'status': group['status'], 'filters': group['filters'],
        'candidate_order_ids': ids, 'list_complete': group['list_complete'], 'total': len(ids),
        'selected_orders': len(submitted), 'rows': rows, 'batches': batches, 'stage': stage,
        'error': (group['error'] or '该范围部分财务未采集') if incomplete else None,
        'missing_order_ids': missing, 'failed_pages': group['failed_pages'],
        'excluded_rows': [row for row in group.get('excluded_rows', []) if row.get('order_id') in selected],
        'loss_rows': [row for row in group['loss_rows'] if row['订单编号'] in selected],
        'blocked_shops': {order: row for order, row in group.get('blocked_shops', {}).items() if order in selected},
    }


def _seller_source(runtime, capture, source, orders):
    if source is None:
        return None
    source_id = f"{source['source_id'].rsplit('-', 1)[0]}-{uuid4().hex}"
    directory = Path(runtime) / 'erp-roster-evidence' / source_id
    path = directory / 'result.json'
    result = {key: source[key] for key in ('schema_version', 'synthetic', 'sample_only', 'queried_at', 'completed_at')}
    result.update(source_id=source_id, erp_capture_id=capture['capture_id'], parent_source_id=source['source_id'],
                  parent_artifact_file=source['artifact_file'], artifact_file=str(path.resolve()),
                  requested_order_ids=sorted(orders), acquisition_complete=source['acquisition_complete'])
    if 'orders' in source:
        result['orders'] = {order: row for order, row in source['orders'].items() if order in orders}
        result['acquisition_complete'] = set(result['orders']) == orders
    else:
        result['batches'] = [{**batch, 'order_ids': subset} for batch in source['batches']
                             if (subset := [order for order in batch['order_ids'] if order in orders])]
        result['rows'] = [row for row in source['rows'] if row['order_id'] in orders]
        observed = {row['order_id'] for row in result['rows']}
        accepted = {order for batch in result['batches'] if batch['status'] in ('complete', 'partial')
                    for order in batch['order_ids']}
        result['acquisition_complete'] = observed == accepted == orders
        result['summary'] = {'requested_orders': len(orders), 'observed_orders': len(observed),
                             'physical_rows': len(result['rows'])}
    directory.mkdir(mode=0o700, parents=True)
    path.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    return result


def project_seller_capture(runtime, parent, headers, native, name):
    groups = [_seller_group(group, name) for group in parent['groups']]
    data = {key: parent[key] for key in ('acquisition_mode', 'source_kind', 'source_version', 'queried_at',
                                        'sample_only', 'department')}
    data.update(synthetic=parent.get('synthetic', False), groups=groups,
                rate_limit_observed=parent.get('rate_limit_observed', False),
                roster_parent_capture_id=parent['capture_id'], salesperson={'name': name},
                capture_complete=all(group['stage'] == 'complete' for group in groups))
    capture = save_capture(runtime, data)
    orders = {order for group in groups for order in group['candidate_order_ids']}
    return (capture, _seller_source(runtime, capture, headers, orders),
            _seller_source(runtime, capture, native, orders))
