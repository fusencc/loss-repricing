"""销售记录页的只读投影；不调用采集、调价或通知，不返回原始证据。"""
from collections import Counter
from sqlalchemy import select
from app.models import Batch, Item, Execution, Ownership, UserAccount
from app.services.coordination import ownership_scopes

KEY_FIELDS = ('platform', 'shop_id', 'site', 'product_id', 'item_id', 'sku', 'warehouse')
PRICE_FIELDS = ('uploaded_tax_inclusive', 'tax_exclusive', 'actual_tax_inclusive', 'marketing')


def display_text(value):
    return value if isinstance(value, str) and value.strip() else None


def record_department(item):
    """按判定当时留存的完整在线商品键关联部门，不借主管/操作人推断归属。

    online_rows 是领域引擎为所有判定保留的规范快照，部门源于销售父商品 department。
    同商品多行有空值或矛盾时保留未知，不能挑一个部门；内部费用及来源行不对外输出。
    """
    identity = tuple(item.key.get(k) for k in KEY_FIELDS)
    trace = (item.decision or {}).get('trace') or {}
    departments = [display_text((row.get('profit_check') or {}).get('department'))
        for row in trace.get('online_rows', [])
        if tuple((row.get('key') or {}).get(k) for k in KEY_FIELDS) == identity]
    unique = set(departments)
    name = departments[0] if departments and len(unique) == 1 and None not in unique else None
    # 销售来源部门前缀实际为 TIKTOK；界面统一显示 Tiktok，不改变归属。
    return 'Tiktok' + name[6:] if name and name[:6].lower() == 'tiktok' else name


def record_seller(item, ownerships, names):
    # 与现有权限规则共用 scope 顺序：目标级优先，其次店铺/站点；这里只展示，不改变授权。
    owner = next((ownerships[s] for s in ownership_scopes(item.key) if s in ownerships), None)
    if owner is None:
        return {'id': None, 'name': '待分配'}
    name = '管理员' if owner.owner_id == 'admin' else names.get(owner.owner_id)
    return {'id': owner.owner_id, 'name': name} if name else {'id': None, 'name': '待分配'}


def public_record(item, batch, execution, department, seller):
    decision = item.decision or {}
    trace = decision.get('trace') or {}
    # 人工真实执行把站点币种保存在同商品 online_rows.currency；仅使用完整键匹配且一致的来源。
    currencies = {display_text(row.get('currency')) for row in trace.get('online_rows', [])
                  if all((row.get('key') or {}).get(k) == item.key.get(k) for k in KEY_FIELDS)}
    currency = display_text(trace.get('station_currency'))
    if currency is None and len(currencies) == 1 and None not in currencies:
        currency = next(iter(currencies))
    # 只展示算价选中的 ERP 订单级修正一次毛利，保留原币、原金额和符号；不使用单件亏损或在线利润。
    selected_id = decision.get('selected_row_id')
    selected = [row for row in trace.get('source_rows', []) if selected_id and row.get('row_id') == selected_id]
    first_margin = None
    if len(selected) == 1:
        row = selected[0]
        if row.get('basis') == 'order' and row.get('fee_scope') == 'erp-corrected-first-margin-v1':
            amount, original_currency = display_text(row.get('loss_amount')), display_text(row.get('currency'))
            if amount is not None and original_currency is not None:
                first_margin = {'amount': amount, 'currency': original_currency}
    evidence = execution.evidence or {} if execution else {}
    archive = evidence.get('price_record') or {}
    # 金额始终为保存时的十进制文本。目标/建议上传与原提交意图/实际回读严格分开。
    # 四价观察时间只描述该次回读，不冒充调价生效时间。
    prices = {name: {field: display_text(value.get(field))
                        for field in ('amount', 'currency', 'observed_at')}
              for name in PRICE_FIELDS
              if isinstance(value := (archive.get('prices') or {}).get(name), dict)}
    # 只展示算价选中的 ERP 订单修正一次毛利：保留原币、订单总额及负号，不用换汇或单件亏损替代。
    selected = display_text(decision.get('selected_row_id'))
    margin_rows = [row for row in trace.get('source_rows', []) if selected and row.get('row_id') == selected]
    margin = None
    if len(margin_rows) == 1:
        row = margin_rows[0]
        if row.get('fee_scope') == 'erp-corrected-first-margin-v1' and row.get('basis') == 'order':
            amount = display_text(row.get('loss_amount'))
            if amount is not None:
                margin = {'amount': amount, 'currency': display_text(row.get('currency'))}
    reconciliation = (batch.snapshot or {}).get('erp_reconciliation') or {}
    return {
        'id': item.id, 'batch_id': item.batch_id,
        'key': {field: item.key.get(field, '') for field in KEY_FIELDS},
        'seller': seller, 'department': department, 'shop_name': display_text(archive.get('shop_name')),
        'first_margin': first_margin,
        'status': item.status, 'reason': item.reason, 'updated_at': item.updated_at,
        'decision': {field: display_text(decision.get(field))
                     for field in ('current_actual', 'target_actual', 'upload_price')} | {
            'trace': {'station_currency': currency}
        } if decision else None,
        'corrected_first_margin': margin,
        'execution': {
            'state': execution.state, 'task_id': execution.task_id,
            'uploaded_price': display_text((execution.request or {}).get('upload_price')),
            'actual_price': display_text(evidence.get('actual_price')),
            'confirmed_at': display_text(evidence.get('confirmed_at')),
            'effective_at': display_text(evidence.get('effective_at')),
            'effective_time_source': display_text(evidence.get('effective_time_source')),
            'updated_at': execution.updated_at, 'prices': prices,
        } if execution else None,
        'business_date': batch.business_date, 'mode': batch.mode,
        'synthetic': batch.mode == 'demo' or bool(reconciliation.get('synthetic')),
    }


def combine_pricing_process(items):
    """同日同完整目标、同方案的准备/失败尝试归入唯一成功结果，不合并多次成功调价。"""
    groups = {}
    for item in items:
        decision = item.get('decision') or {}
        identity = (item['business_date'], item['mode'],
                    *(item['key'].get(k) for k in KEY_FIELDS),
                    *(decision.get(k) for k in ('current_actual', 'target_actual', 'upload_price')))
        groups.setdefault(identity, []).append(item)
    hidden = set()
    for group in groups.values():
        successes = [i for i in group if i.get('execution') and i['execution']['state'] == 'verified']
        if len(successes) != 1:
            continue
        final = successes[0]
        attempts = [i for i in group if i is final or (
            i['updated_at'] <= final['updated_at'] and
            (i.get('execution') is None or i['execution']['state'] == 'failed'))]
        if len(attempts) < 2:
            continue
        final['process'] = [{'id': i['id'], 'updated_at': i['updated_at'],
                             'stage': '提价成功' if i is final else '提交失败' if i.get('execution') else '准备提价',
                             'reason': i['reason']} for i in reversed(attempts)]
        hidden.update(i['id'] for i in attempts if i is not final)
    return [i for i in items if i['id'] not in hidden]


def department_records(session, department=None, *, mode=None):
    """全部已保存批次，不按 SKU 去重，不截断；未知部门有独立选项。"""
    query = (select(Item, Batch, Execution).join(Batch, Item.batch_id == Batch.id)
        .outerjoin(Execution, Execution.item_id == Item.id)
        .order_by(Item.updated_at.desc(), Batch.business_date.desc(), Item.id.desc()))
    if mode is not None:
        query = query.where(Batch.mode == mode)
    rows = [(item, batch, execution, record_department(item))
            for item, batch, execution in session.execute(query)]
    counts = Counter(row[3] for row in rows)
    fixed_names = ['Tiktok一部', 'Tiktok五部', 'Tiktok十二部', 'Tiktok二十二部']
    names_in_records = sorted(name for name in counts if name is not None and name not in fixed_names)
    departments = [{'id': 'department:' + name, 'name': name, 'total': counts[name]}
                   for name in fixed_names + names_in_records]
    if None in counts:
        departments.append({'id': 'unassigned', 'name': '待确认部门', 'total': counts[None]})
    if department is not None and department not in {d['id'] for d in departments}:
        raise ValueError('所选部门不存在或已没有记录，请重新打开页面选择部门')
    selected = department if department is not None else departments[0]['id'] if departments else None
    chosen = [row for row in rows if ('department:' + row[3] if row[3] is not None else 'unassigned') == selected]
    ownerships = {row.scope_key: row for row in session.scalars(select(Ownership))}
    names = dict(session.execute(select(UserAccount.id, UserAccount.display_name)).all())
    items = combine_pricing_process([public_record(item, batch, execution, dept, record_seller(item, ownerships, names))
                                    for item, batch, execution, dept in chosen])
    for entry in departments:
        if entry['id'] == selected:
            entry['total'] = len(items)
    return {
        'departments': departments, 'department': selected, 'total': len(items),
        'counts': dict(Counter(item['status'] for item in items)),
        'shops': len({(item.key.get('platform'), item.key.get('shop_id')) for item, _, _, _ in chosen}),
        'items': items,
    }
