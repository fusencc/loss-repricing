"""销售记录页的只读投影；不调用采集、调价或通知，不返回原始证据。"""
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from sqlalchemy import select
from app.models import Batch, Item, Execution, Ownership, UserAccount
from app.services.coordination import ownership_scopes

KEY_FIELDS = ('platform', 'shop_id', 'site', 'product_id', 'item_id', 'sku', 'warehouse')


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


# 展示名按用户口径：② 补亏损、③ 利润校验。③ 相对②的增加额即在线利润校正额，单列展示。
STAGE_LABELS = (
    ('original', '① 原实际营销价'),
    ('loss', '② 补亏损'),
    ('break_even', '③ 利润校验'),
)
DISPLAY_QUANT = Decimal('0.0001')
PERCENT_QUANT = Decimal('0.01')


def _decimal(value):
    """读取已保存的十进制文本；空值或非法文本返回 None，不猜默认值。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _display(value, quantum=DISPLAY_QUANT):
    # 展示层固定小数位；业务判定仍使用保存的原始精确值。
    return str(value.quantize(quantum, rounding=ROUND_HALF_UP))


def price_stages(decision, trace, snapshot, item_key):
    """把价格拆成「原实际营销价→只补回订单亏损→利润校验或参考价优先」三环节。

    只使用判定时保存的金额：① 为 decision.current_actual；② 优先取
    trace.online_profit.initial_target，旧判定回退到 current_actual + station_loss；
    ③ 为 decision.target_actual。折合人民币按快照汇率方向换算（cny_per_currency
    直接相乘，旧 currency_per_cny 快照相除）。任一环节缺证据即保留空值，不用
    目标价或当前价顶替，也不在前端重算。增量与涨幅均相对上一个有值的环节；
    「在线利润率导致的变动」取 trace.online_profit.correction_amount（在线利润
    校正额），仅在 ③ 利润校验行输出，缺证据时为 null 不显示。
    """
    current = _decimal(decision.get('current_actual'))
    target = _decimal(decision.get('target_actual'))
    if current is None or target is None:
        return None
    online_profit = trace.get('online_profit') or {}
    initial = _decimal(online_profit.get('initial_target'))
    station_loss = _decimal(decision.get('station_loss'))
    loss_stage = initial if initial is not None else (
        current + station_loss if station_loss is not None else None)

    currency = display_text(trace.get('station_currency'))
    if currency is None:
        # 旧判定没有顶层币种时，仅在同商品在线行币种唯一时采用，不跨商品猜。
        rows = [row for row in trace.get('online_rows') or [] if isinstance(row, dict)]
        currencies = {display_text(row.get('currency')) for row in rows
                      if all((row.get('key') or {}).get(k) == item_key.get(k) for k in KEY_FIELDS)}
        if len(currencies) == 1 and None not in currencies:
            currency = next(iter(currencies))
    snapshot = snapshot or {}
    rate = _decimal((snapshot.get('fx_rates') or {}).get(currency)) if currency else None
    fx_unit = snapshot.get('fx_rate_unit')

    def to_cny(amount):
        if amount is None or rate is None or rate <= 0:
            return None
        # 快照未标注单位的历史数据是「每1CNY可兑换的站点币」，需要相除。
        return amount * rate if fx_unit == 'cny_per_currency' else amount / rate

    values = {'original': current, 'loss': loss_stage, 'break_even': target}
    # 「在线利润率导致的变动」只在 ③ 利润校验行有值：在线利润校验要求目标价高于
    # 初步补亏价时，其差额就是保存的在线利润校正额（correction_amount）；通过校验为 0，
    # 缺证据为 None。与折合人民币列同汇率换算并同精度显示，不在前端重算。
    profit_correction = _decimal((trace.get('online_profit') or {}).get('correction_amount'))
    profit_change_cny = to_cny(profit_correction)
    rows, previous = [], None
    for key, label in STAGE_LABELS:
        if key == 'break_even' and trace.get('price_limit'):
            label = '③ 参考价优先'
        amount = values[key]
        cny = to_cny(amount)
        # 「比上一环节」严格相对表格上一行；上一环节缺失时为 —，不跨行比较制造环比。
        change_cny = to_cny(amount - previous) if amount is not None and previous is not None else None
        change_percent = (None if amount is None or previous is None or previous == 0
                          else (amount - previous) / previous * Decimal(100))
        row = {
            'key': key, 'label': label,
            'price': None if amount is None else _display(amount),
            'cny': None if cny is None else _display(cny),
            'change_cny': None if change_cny is None else _display(change_cny),
            'change_percent': None if change_percent is None else _display(change_percent, PERCENT_QUANT),
            # ③ 行展示在线利润校正额折合人民币；缺证据为 null，显示层不推断为 0。
            'profit_change_cny': None if key != 'break_even' or profit_change_cny is None
                                 else _display(profit_change_cny),
        }
        if key == 'loss' and initial is not None:
            # ② 的价格是 BD 公式的首次试算输入。只展示该次判定保存的结果。
            initial_profit = _decimal(online_profit.get('initial_profit'))
            initial_margin = _decimal(online_profit.get('initial_margin'))
            if initial_profit is not None and initial_margin is not None:
                row['trial_profit'] = _display(initial_profit)
                row['trial_margin_percent'] = _display(initial_margin * 100, PERCENT_QUANT)
        rows.append(row)
        previous = amount

    return {
        'currency': currency,
        'fx_rate': None if rate is None else str(rate),
        'fx_date': display_text(snapshot.get('fx_date')),
        'rows': rows,
    }


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
    # 用户要求记录页不展示四价存档（税前价、平台回读含税价、营销价观察等）；
    # 这些证据仍完整保存在 Execution.evidence.price_record，只从页面投影移除，不删原始证据。
    # 只展示算价选中的 ERP 订单修正一次毛利：保留原币、订单总额及负号，不用换汇或单件亏损替代。
    selected = display_text(decision.get('selected_row_id'))
    margin_rows = [row for row in (trace.get('source_rows') or []) if selected and row.get('row_id') == selected]
    margin = None
    if len(margin_rows) == 1:
        row = margin_rows[0]
        if row.get('fee_scope') == 'erp-corrected-first-margin-v1' and row.get('basis') == 'order':
            amount = display_text(row.get('loss_amount'))
            if amount is not None:
                margin = {'amount': amount, 'currency': display_text(row.get('currency'))}
    # 价格变动 = 目标实际价 - 调整前实际价，与 decision 两价同币种（站点币）；
    # 任一价缺失或无效则不显示，不猜测。用 Decimal 保持与保存金额同精度，正数带 + 号由前端展示。
    price_change = None
    current, target = decision.get('current_actual'), decision.get('target_actual')
    if isinstance(current, str) and current.strip() and isinstance(target, str) and target.strip():
        try:
            price_change = str(Decimal(target) - Decimal(current))
        except InvalidOperation:
            price_change = None
    reconciliation = (batch.snapshot or {}).get('erp_reconciliation') or {}
    return {
        'id': item.id, 'batch_id': item.batch_id,
        'key': {field: item.key.get(field, '') for field in KEY_FIELDS},
        'seller': seller, 'department': department, 'shop_name': display_text(archive.get('shop_name')),
        'first_margin': first_margin,
        # 仅投影本条持久阶段与结果类型；原因仍用本条商品记录，不读取其他尝试或推断查询异常。
        'status': item.status, 'phase': item.phase, 'reason': item.reason, 'updated_at': item.updated_at,
        'decision': {field: display_text(decision.get(field))
                     for field in ('current_actual', 'target_actual', 'upload_price')} | {
            'trace': {'station_currency': currency}, 'price_change': price_change,
            # 三环节价格表由后端用保存金额与当日汇率算出，前端只按行展示。
            'stages': price_stages(decision, trace, batch.snapshot, item.key) if decision else None,
            'sku_price_reference': trace.get('sku_price_reference'),
            'price_limit': trace.get('price_limit'),
        } if decision else None,
        'corrected_first_margin': margin,
        'execution': {
            'state': execution.state, 'task_id': execution.task_id,
            'submit_status': display_text((execution.response or {}).get('status')),
            'row_status': display_text(evidence.get('row_status')),
            'uploaded_price': display_text((execution.request or {}).get('upload_price')),
            'actual_price': display_text(evidence.get('actual_price')),
            'confirmed_at': display_text(evidence.get('confirmed_at')),
            'effective_at': display_text(evidence.get('effective_at')),
            'effective_time_source': display_text(evidence.get('effective_time_source')),
            'updated_at': execution.updated_at,
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
