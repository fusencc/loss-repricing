"""销售记录页的只读投影；不调用采集、调价或通知，不返回原始证据。"""
from collections import Counter
import logging
from functools import lru_cache
from types import SimpleNamespace
from app.config import get_settings
from app.services.erp_capture_store import load_capture

logger = logging.getLogger(__name__)
from decimal import Decimal, InvalidOperation
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.models import Batch, Item, Execution, Ownership, UserAccount
from app.services.coordination import ownership_scopes
from app.services.decimal_display import format_decimal

KEY_FIELDS = ('platform', 'shop_id', 'site', 'product_id', 'item_id', 'sku', 'warehouse')


def display_text(value):
    return value if isinstance(value, str) and value.strip() else None


def record_display_time(item, evidence):
    # 已绑定本次执行的成功调价日志 completeTime 是北京时间任务完成时间，不能使用核验时间或提交意图时间代替。
    log = (evidence.get('price_record') or {}).get('operation_log') or {}
    value = display_text(log.get('completeTime')) if log.get('status') == 2 else None
    if value:
        try:
            parsed = datetime.fromisoformat(value)
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai'))).isoformat()
        except ValueError:
            pass
    # 无可靠上传完成时间时取相同完整商品键的在线部门来源采集时间；冲突或缺失不猜测。
    rows = ((item.decision or {}).get('trace') or {}).get('online_rows') or []
    times = {display_text(row.get('observed_at')) for row in rows
             if all((row.get('key') or {}).get(k) == item.key.get(k) for k in KEY_FIELDS)}
    return next(iter(times)) if len(times) == 1 and None not in times else None


def roster_record_source(batch):
    """仅已绑定业务日及销售员的真实名单批次使用其留存归属。"""
    source = (batch.snapshot or {}).get('source_versions') or {}
    seller = display_text(source.get('erp_roster_salesperson'))
    if (batch.mode == 'live' and seller and batch.run_key.startswith('erp-roster:')
            and batch.run_key.endswith(f':{seller}:{batch.business_date}')):
        return source
    return {}


def record_department(item, batch):
    """名单使用已校验批次部门；其他记录按完整在线商品键关联销售来源部门。

    online_rows 是领域引擎为所有判定保留的规范快照，部门源于销售父商品 department。
    普通记录同商品多行有空值或矛盾时保留未知，不借主管/操作人推断归属。
    """
    # 名单及库存补算的部门来自本批经校验的 ERP 范围；部分成功回读重建了在线快照，
    # 未携带费用部门，不应因此把已有明确批次归属的成功记录隐藏到未知部门。
    department = ((item.decision or {}).get('trace', {}).get('erp_department') or {}).get('department')
    if department and batch.mode == 'live' and batch.run_key.startswith('erp-department:'):
        return department
    roster_department = display_text(roster_record_source(batch).get('erp_roster_department'))
    if roster_department:
        return ('Tiktok' + roster_department[6:] if roster_department[:6].lower() == 'tiktok'
                else roster_department)
    identity = tuple(item.key.get(k) for k in KEY_FIELDS)
    trace = (item.decision or {}).get('trace') or {}
    departments = [display_text((row.get('profit_check') or {}).get('department'))
        for row in trace.get('online_rows', [])
        if tuple((row.get('key') or {}).get(k) for k in KEY_FIELDS) == identity]
    unique = set(departments)
    name = departments[0] if departments and len(unique) == 1 and None not in unique else None
    # 销售来源部门前缀实际为 TIKTOK；界面统一显示 Tiktok，不改变归属。
    return 'Tiktok' + name[6:] if name and name[:6].lower() == 'tiktok' else name


def record_seller(item, batch, ownerships, names):
    department = (item.decision or {}).get('trace', {}).get('erp_department') or {}
    if batch.mode == 'live' and batch.run_key.startswith('erp-department:') and department:
        seller = display_text(department.get('salesperson'))
        return {'id': f'erp:{seller}', 'name': seller} if seller else {'id': None, 'name': '待分配'}
    # 名单批次保存了本次经校验的 ERP 销售员。普通提价和库存补算按同一个名字归档。
    source = roster_record_source(batch)
    roster_seller = display_text(source.get('erp_roster_salesperson'))
    if roster_seller:
        return {'id': f'erp-roster:{roster_seller}', 'name': roster_seller}
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


def _decimal(value):
    """读取已保存的十进制文本；空值或非法文本返回 None，不猜默认值。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


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
            'price': format_decimal(amount),
            'cny': format_decimal(cny),
            'change_cny': format_decimal(change_cny),
            'change_percent': format_decimal(change_percent),
            # ③ 行展示在线利润校正额折合人民币；缺证据为 null，显示层不推断为 0。
            'profit_change_cny': format_decimal(profit_change_cny) if key == 'break_even' else None,
        }
        if key == 'loss' and initial is not None:
            # ② 的价格是 BD 公式的首次试算输入。只展示该次判定保存的结果。
            initial_profit = _decimal(online_profit.get('initial_profit'))
            initial_margin = _decimal(online_profit.get('initial_margin'))
            if initial_profit is not None and initial_margin is not None:
                row['trial_profit'] = format_decimal(initial_profit)
                row['trial_margin_percent'] = format_decimal(initial_margin * 100)
        rows.append(row)
        previous = amount

    return {
        'currency': currency,
        'fx_rate': format_decimal(rate),
        'fx_date': display_text(snapshot.get('fx_date')),
        'rows': rows,
    }


def selected_order_first_margin(decision):
    """同一选中 ERP 订单可来自多个候选列表；一致证据只展示一次原毛利，不累加。"""
    selected_id = display_text(decision.get('selected_row_id'))
    rows = [row for row in (decision.get('trace') or {}).get('source_rows', [])
            if selected_id and row.get('row_id') == selected_id]
    if not rows:
        return None
    # 多来源重复必须仍是同一明确订单；金额、币种或订单粒度有冲突时不能挑一条显示。
    order_ids = {display_text(row.get('order_id')) for row in rows}
    if len(rows) > 1 and (len(order_ids) != 1 or None in order_ids):
        return None
    values = set()
    for row in rows:
        amount = _decimal(row.get('loss_amount'))
        currency = display_text(row.get('currency'))
        if (row.get('basis') != 'order' or row.get('fee_scope') != 'erp-corrected-first-margin-v1'
                or amount is None or not amount.is_finite() or currency is None):
            return None
        values.add((amount, currency))
    if len(values) != 1:
        return None
    # 保留首条来源的十进制文本和原币，不把订单总毛利替换为单件亏损、换汇值或在线利润。
    return {'amount': rows[0]['loss_amount'], 'currency': rows[0]['currency']}


def roster_outcome(item, execution):
    """名单以本条上传受理为准；价格回读保留为证据，不作为再次上传的依据。"""
    if execution is not None:
        submit = (execution.response or {}).get('status')
        if execution.state == 'failed' or submit == 'rejected' or (execution.evidence or {}).get('row_status') == 'rejected':
            return '待处理', item.reason
        if execution.state in {'submitting', 'unknown'}:
            return '待核验', '上传结果待确认，请勿重复上传'
        if execution.state in {'accepted', 'uploaded', 'verified'} or submit == 'accepted':
            return '已提价', '提价上传完成'
        if execution.state == 'mismatch':
            return '待核验', '上传结果待确认，请勿重复上传'
    elif (item.decision or {}).get('reason_code') == 'pending_submission':
        return '待核验', '已有未完成的提交，请勿重复上传'
    return item.status, item.reason


def public_record(item, batch, execution, department, seller):
    # 已执行记录的算价依据属于提交时保存的不可变请求，不属于后来重评的当前候选。
    # 只有完整商品键一致才使用该执行档案；回读金额仍独立取 execution.evidence。
    request = (execution.request or {}) if execution else {}
    bound_request = request.get('key') or {}
    decision = (request if bound_request and all(bound_request.get(k) == item.key.get(k) for k in KEY_FIELDS)
                else item.decision or {})
    trace = decision.get('trace') or {}
    # 人工真实执行把站点币种保存在同商品 online_rows.currency；仅使用完整键匹配且一致的来源。
    currencies = {display_text(row.get('currency')) for row in trace.get('online_rows', [])
                  if all((row.get('key') or {}).get(k) == item.key.get(k) for k in KEY_FIELDS)}
    currency = display_text(trace.get('station_currency'))
    if currency is None and len(currencies) == 1 and None not in currencies:
        currency = next(iter(currencies))
    first_margin = selected_order_first_margin(decision)
    evidence = execution.evidence or {} if execution else {}
    archive = evidence.get('price_record') or {}
    shop_name = display_text(archive.get('shop_name')) or display_text((trace.get('erp_department') or {}).get('shop_name'))
    if not shop_name and decision is request:
        authorization = (trace.get('erp_roster_upload') or {}).get('authorization') or {}
        shop_name = display_text(authorization.get('shop_name'))
    # 用户要求记录页不展示四价存档（税前价、平台回读含税价、营销价观察等）；
    # 这些证据仍完整保存在 Execution.evidence.price_record，只从页面投影移除，不删原始证据。
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
    status, reason = roster_outcome(item, execution) if roster_record_source(batch) else (item.status, item.reason)
    return {
        'id': item.id, 'batch_id': item.batch_id,
        'key': {field: item.key.get(field, '') for field in KEY_FIELDS},
        'seller': seller, 'department': department, 'shop_name': shop_name,
        'first_margin': first_margin,
        # 仅投影本条持久阶段与结果类型；原因仍用本条商品记录，不读取其他尝试或推断查询异常。
        'status': status, 'phase': item.phase, 'reason': reason, 'updated_at': item.updated_at,
        'display_time': record_display_time(item, evidence),
        'decision': {field: display_text(decision.get(field))
                     for field in ('current_actual', 'target_actual', 'upload_price')} | {
            'trace': {'station_currency': currency}, 'price_change': price_change,
            # 三环节价格表由后端用保存金额与当日汇率算出，前端只按行展示。
            'stages': price_stages(decision, trace, batch.snapshot, item.key) if decision else None,
            'sku_price_reference': trace.get('sku_price_reference'),
            'price_limit': trace.get('price_limit'),
        } if decision else None,
        'corrected_first_margin': first_margin,
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
        successes = [i for i in group if i.get('execution') and (
            i['execution']['state'] not in {'failed', 'unknown', 'submitting'}
            and i['execution'].get('row_status') != 'rejected'
            and i['execution'].get('submit_status') != 'rejected'
            and (i.get('status') == '已提价' or i['execution']['state'] in {'accepted', 'uploaded', 'verified'}
                 or (i['execution']['state'] != 'mismatch' and i['execution'].get('submit_status') == 'accepted')))]
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


def department_records(session, department=None, *, mode=None, runtime_dir=None):
    """全部已保存批次，不按 SKU 去重，不截断；未知部门有独立选项。"""
    # A batch snapshot can contain thousands of products. Read its display fields
    # once per batch instead of decoding the whole snapshot once per item.
    fields = ('source_versions', 'erp_roster_filter_log', 'erp_reconciliation',
              'fx_rates', 'fx_date', 'fx_rate_unit')
    query = select(Batch.id, Batch.run_key, Batch.business_date, Batch.mode,
                   *(Batch.snapshot[key].label(key) for key in fields))
    if mode is not None:
        query = query.where(Batch.mode == mode)
    batches = {
        row.id: SimpleNamespace(id=row.id, run_key=row.run_key, business_date=row.business_date,
                                mode=row.mode, snapshot={key: row._mapping[key] for key in fields})
        for row in session.execute(query)
    }
    item_query = (select(Item, Batch.id).join(Batch, Item.batch_id == Batch.id)
                  .order_by(Item.updated_at.desc(), Batch.business_date.desc(), Item.id.desc()))
    if mode is not None:
        item_query = item_query.where(Batch.mode == mode)
    rows = [(item, batches[batch_id], record_department(item, batches[batch_id]))
            for item, batch_id in session.execute(item_query) if batch_id in batches]
    counts = Counter(row[2] for row in rows)
    fixed_names = ['Tiktok一部', 'Tiktok五部', 'Tiktok十二部', 'Tiktok二十二部']
    names_in_records = sorted(name for name in counts if name is not None and name not in fixed_names)
    departments = [{'id': 'department:' + name, 'name': name, 'total': counts[name]}
                   for name in fixed_names + names_in_records]
    if None in counts:
        departments.append({'id': 'unassigned', 'name': '待确认部门', 'total': counts[None]})
    if department is not None and department not in {d['id'] for d in departments}:
        raise ValueError('所选部门不存在或已没有记录，请重新打开页面选择部门')
    selected = department if department is not None else departments[0]['id'] if departments else None
    chosen = [row for row in rows if ('department:' + row[2] if row[2] is not None else 'unassigned') == selected]
    ownerships = {row.scope_key: row for row in session.scalars(select(Ownership))}
    names = dict(session.execute(select(UserAccount.id, UserAccount.display_name)).all())
    @lru_cache(maxsize=None)
    def capture_time(batch_id):
        batch = batches[batch_id]
        source = (batch.snapshot or {}).get('erp_roster_filter_log') or {}
        versions = roster_record_source(batch)
        capture_id = source.get('capture_id')
        if not capture_id or capture_id != versions.get('erp_capture_id'):
            return None
        try:
            capture = load_capture(runtime_dir if runtime_dir is not None else get_settings().runtime_dir, capture_id)
            if (capture.get('capture_id') != capture_id or capture.get('source_kind') != 'erp_web_backend'
                    or capture.get('synthetic', False) is not False or capture.get('sample_only') is not False
                    or capture.get('department') != source.get('department')
                    or (capture.get('salesperson') or {}).get('name') != source.get('salesperson')
                    or source.get('salesperson') != versions.get('erp_roster_salesperson')
                    or source.get('department') != versions.get('erp_roster_department')):
                return None
            value = display_text(capture.get('queried_at'))
            if value and datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo:
                return value
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logger.warning('采集时间读取失败，时间显示为空：%s', type(exc).__name__)
        return None

    projected = []
    executions = {}
    for offset in range(0, len(chosen), 500):
        ids = [row[0].id for row in chosen[offset:offset + 500]]
        executions.update((e.item_id, e) for e in session.scalars(
            select(Execution).where(Execution.item_id.in_(ids))))
    for item, batch, dept in chosen:
        execution = executions.get(item.id)
        record = public_record(item, batch, execution, dept, record_seller(item, batch, ownerships, names))
        # 名单保护记录仅关联其本人同部门采集归档的 queried_at；不使用入库时间冒充采集时间。
        if record['display_time'] is None and item.status == '不调价':
            record['display_time'] = capture_time(batch.id)
        projected.append(record)
    items = combine_pricing_process(projected)
    for entry in departments:
        if entry['id'] == selected:
            entry['total'] = len(items)
    # 无法完成采集的人员没有可信商品键，单独显示范围异常，不伪造商品或零保护数量。
    scope_issues = []
    for saved in batches.values():
        if not saved.run_key.startswith('erp-roster:') or ':history:' not in saved.run_key:
            continue
        source = (saved.snapshot or {}).get('erp_roster_filter_log') or {}
        if (source and source.get('capture_complete') is False
                and 'department:' + source['department'] == selected):
            scope_issues.append({'salesperson': source['salesperson'], 'department': source['department'],
                'business_date': saved.business_date, 'capture_id': source['capture_id'],
                'reason': '采集未完成，三天调价保护数量无法确认'})
    return {
        'scope_issues': scope_issues,
        'departments': departments, 'department': selected, 'total': len(items),
        'counts': dict(Counter(item['status'] for item in items)),
        'shops': len({(item.key.get('platform'), item.key.get('shop_id')) for item, _, _ in chosen}),
        'items': items,
    }
