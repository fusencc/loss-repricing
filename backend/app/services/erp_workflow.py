"""ERP 候选至待提价文件的只读编排；复用统一规则，不调用模型或价格上传。"""
from collections import Counter
from datetime import date, datetime
from app.services.store import BEIJING
from pathlib import Path
from uuid import uuid4
import json
from hashlib import sha256
import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.normalization import source_time
from app.adapters.erp_bulk_capture import ERP_DEPARTMENTS, _session, capture_erp_bulk
from app.adapters.sales_auth import SalesAuth
from app.adapters.erp_web import ErpAccountRateLimited
from app.adapters.erp_rates import query_current_erp_fx
from app.adapters.erp_detail_capture import capture_erp_details
from app.adapters.erp_order_loss import prepare_order_losses
from app.adapters.erp_order_headers import capture_order_headers
from app.adapters.current_pricing_fees import capture_current_fees, refresh_daily_fees
from app.adapters.sales_price_reference import capture_sku_price_references
from app.adapters.erp_pricing import prepare_pricing
from app.services.local_price_history import capture_local_price_history
from app.adapters.official_price_logs import capture_recent_price_changes, recent_price_log_for
from app.domain.types import VariantKey
from app.observability import event
from decimal import Decimal
from app.adapters.sales_batch_export import capture_sales_batch, export_activity_hints, select_export_candidates
from app.adapters.sales_platform_capture import capture_platform_controls
from app.adapters.live_online_preparation import prepare_online_states
from app.services.erp_capture_store import load_capture, rebuild_clean_capture, save_capture, write_verified
from app.services.erp_pricing import load_pricing_preview, save_pricing_preview
from app.services.erp_seller_candidates import summarize_seller_candidates

def filter_candidate_history(settings, targets, *, client=None, auth=None):
    """按子商品查询近72小时官方历史；仅明确有调价记录时排除，未知继续。"""
    history = (capture_recent_price_changes(settings,[VariantKey.model_validate(t['key']) for t in targets],
                                             client=client, auth=auth)
        if targets else None)
    eligible, filtered = [], []
    for target in targets:
        key=VariantKey.model_validate(target['key'])
        try:
            count=recent_price_log_for(history,key).count
            status='history_protected' if count>0 else 'passed'
        except ValueError:
            count,status=None,'history_unknown'
        filtered.append({'key':target['key'],'count':count,'status':status})
        if status!='history_protected':eligible.append(target)
    return history,eligible,filtered


# 2026-11-24 用户确认：11月10日至24日历史财务表清洗后的「销售员」「部门」
# 字段显示这七人各自仅属于一个部门，无跨部门冲突；据此只采集各人的已确认部门，
# 不再与四部门交叉组合。部门值使用既有 ERP 筛选合同的原始名称。
# 这是取数范围，不是网页归属、店铺试点或真实价格提交授权；四类来源仍逐一读取。
ERP_ROSTER_DEPARTMENTS = {
    '桂露': 'Tiktok二十二部',
    '莫雯舒': 'Tiktok一部',
    '赵亮亮': 'Tiktok十二部',
    '刘付文昌': 'Tiktok一部',
    '李佳丽': 'Tiktok二十二部',
    '杨颖': 'Tiktok十二部',
    '曹雪娇': 'Tiktok五部',
}
ERP_ROSTER_NAMES = tuple(ERP_ROSTER_DEPARTMENTS)


def _roster(settings, required_day):
    today = datetime.now(BEIJING).date()
    if required_day is not None and required_day != today:
        raise ValueError('名单任务不属于当前北京时间业务日')
    if required_day is None and settings.erp_roster_date != today:
        return None
    names = ERP_ROSTER_NAMES
    if not isinstance(names, tuple) or not names or any(not isinstance(n, str) or not n.strip() for n in names):
        raise ValueError('代码中的销售员名单须为非空姓名元组，不进行 ERP 采集')
    return list(dict.fromkeys(n.strip() for n in names))


def run_workflow(settings, *, capture_id=None, evidence=None, exports=None, progress=None,
                 roster_date: date | None = None, roster_person=None, on_roster_scope=None):
    """按部门串行完成只读采集和算价，显式指定采集编号时回放单批。"""
    if roster_person is not None and (roster_date is None or roster_person not in ERP_ROSTER_DEPARTMENTS):
        raise ValueError('名单刷新须绑定单日名单中的销售员')
    if capture_id is not None:
        if roster_date is not None:
            raise ValueError('名单采集不能与离线回放同时指定')
        rebuild_clean_capture(settings.runtime_dir, capture_id)
        return _evaluate_capture(settings, capture_id, replay=True, evidence=evidence, exports=exports, progress=progress)
    if evidence is not None or exports is not None:
        raise ValueError('补充文件只可用于明确指定采集编号的离线对账')

    names = _roster(settings, roster_date)
    if roster_person is not None:
        if roster_person not in names:
            raise ValueError('刷新销售员不在本轮名单中')
        names = [roster_person]
    # 名单与部门归属先于登录和公共费用取数校验；缺少归属不得扩大为跨部门采集。
    if names is not None:
        if any(ERP_ROSTER_DEPARTMENTS.get(name) not in ERP_DEPARTMENTS for name in names):
            raise ValueError('代码中的销售员名单缺少有效部门归属，不进行 ERP 采集')
        scopes = [(ERP_ROSTER_DEPARTMENTS[name], name) for name in names]
    else:
        scopes = [(department, None) for department in ERP_DEPARTMENTS]
    directory = Path(settings.runtime_dir) / 'erp-workflows' / uuid4().hex
    directory.mkdir(mode=0o700, parents=True)
    departments = []
    summary = {
        'mode': 'department_capture', 'status': 'running', 'execution_allowed': False,
        'department_order': list(ERP_DEPARTMENTS), 'departments': departments,
        'ready_prices': 0, 'artifact_dir': str(directory.resolve()),
    }
    if names is not None:
        summary['salespersons'] = names
        summary['roster_date'] = datetime.now(BEIJING).date().isoformat()
    manifest = directory / 'result.json'
    write_verified(manifest, summary)
    with (httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client,
          _session(settings) as web_session):
        auth = SalesAuth(settings, client)
        for department, name in scopes:
            if progress:
                progress(f'开始处理 {department}。')
            try:
                result = _run_workflow(settings, department=department, progress=progress,
                                       web_session=web_session, client=client, auth=auth,
                                       **({'salesperson': name} if name is not None else {}))
            except Exception as error:
                # 单部门异常在调度边界留痕，不能终止其他独立部门。
                result = _workflow_failure(error)
            departments.append({**result, 'department': department,
                                **({'salesperson': name} if name is not None else {})})
            summary['ready_prices'] += result['ready_prices']
            write_verified(manifest, summary)
            if name is not None and on_roster_scope is not None:
                on_roster_scope(departments[-1])
            if progress:
                progress(f"{department} 处理结果：{result['status']}。")
            if result.get('stopped_by_rate_limit'):
                # 账号级 500003 影响整次任务，不继续请求其他部门或销售员。
                break
    if len(departments) == len(scopes) and all(department['status'] == 'completed' for department in departments):
        summary['status'] = 'completed'
    elif summary['ready_prices'] or any(department['status'] == 'completed' for department in departments):
        summary['status'] = 'needs_attention'
    else:
        summary['status'] = 'blocked'
    write_verified(manifest, summary)
    return summary


def _workflow_failure(error):
    error_id = event('erp.failed', exception=error, source='erp', synthetic=False,
                     stage='collect', error_code='workflow_failed')
    return {'status': 'failed', 'error_id': error_id, 'ready_prices': 0, 'execution_allowed': False,
            'stopped_by_rate_limit': isinstance(error, ErpAccountRateLimited)}


def _run_workflow(settings, *, department, web_session, client, auth, progress=None, salesperson=None):
    # 每批只绑定启动时的当日检查；同日后续批次再补查失败的范围。
    daily_fees = refresh_daily_fees(settings, client=client, auth=auth)
    collected = capture_erp_bulk(settings, department=department, progress=progress,
                                 web_session=web_session, salesperson=salesperson)
    saved = save_capture(settings.runtime_dir, collected)
    capture_id = saved['capture_id']
    try:
        # 原件成功但清洗失败时不得让后续空文件看起来像已完成。
        if saved['cleaning_status'] != 'saved':
            raise OSError('原件已存档，清洗失败；请按采集编号重建清洗')
        return _evaluate_capture(settings, capture_id, replay=False, progress=progress, daily_fees=daily_fees,
                                 web_session=web_session, client=client, auth=auth)
    except Exception as error:
        # 算价或清洗失败时，仍保留已经写入的采集编号供核对。
        return {**_workflow_failure(error), 'capture_id': capture_id, 'department': department,
                'capture_complete': saved['capture_complete'], 'cleaning_status': saved['cleaning_status'],
                'stopped_by_rate_limit': saved.get('stopped_by_rate_limit', False) or isinstance(error, ErpAccountRateLimited)}


def _evaluate_capture(settings, capture_id, *, replay, evidence=None, exports=None, progress=None, daily_fees=None,
                      web_session=None, client=None, auth=None):
    capture = load_capture(settings.runtime_dir, capture_id)
    limited = capture.get('stopped_by_rate_limit', False)
    empty = capture['capture_complete'] and not any(group['candidate_order_ids'] for group in capture['groups'])
    if not replay and (limited or empty):
        # 账号限流时停止本部门的后续取证。完整空名单已经确认无需进一步取数。
        return {
            'capture_id': capture_id, 'department': capture['department'], 'mode': 'capture',
            'status': 'blocked' if limited else 'completed',
            'capture_complete': capture['capture_complete'], 'cleaning_status': 'saved',
            'stopped_by_rate_limit': limited, 'ready_prices': 0, 'execution_allowed': False,
        }
    current_fx = None
    sales_ref = None
    sales = None
    native_ref = None
    headers = None
    headers_ref = None
    headers_status = 'not_requested'
    platform_ref = None
    platform_status = 'not_requested'
    local_history_ref = None
    local_history_status = 'not_requested'
    fixed_fees = None
    fixed_fees_ref = None
    order_preparation = None
    online_preparation = None
    platform = None
    pricing_targets = []
    history_filter = []
    official_history = None
    native_status = 'not_requested'
    sales_status = 'not_requested'
    fx_status = 'file_evidence' if evidence is not None else 'not_requested'
    if not replay:
        headers = capture_order_headers(settings, capture, progress=progress, web_session=web_session)
        content = Path(headers['artifact_file']).read_bytes()
        headers = json.loads(content)
        headers_ref = {key:headers[key] for key in ('source_id','erp_capture_id','synthetic','sample_only',
            'acquisition_complete','artifact_file')}
        headers_ref['sha256'] = sha256(content).hexdigest()
        headers_status = 'saved' if headers['acquisition_complete'] else 'partial'
        try:
            native = capture_erp_details(settings, capture, progress=progress, web_session=web_session)
        except SourceUnavailable:
            native_status = 'unavailable'
        else:
            content = Path(native['artifact_file']).read_bytes()
            native = json.loads(content)
            native_ref = {key:native[key] for key in ('source_id','erp_capture_id','synthetic','sample_only',
                          'acquisition_complete','summary','artifact_file')}
            native_ref['sha256'] = sha256(content).hexdigest()
            native_status = 'saved' if native['acquisition_complete'] else 'partial'
            # 先核对同订单的财务、明细和店铺，再按负修正一次毛利圈定导出范围。
            erp_only = prepare_order_losses(capture, native, order_headers=headers)
            candidates = select_export_candidates(capture, erp_only['proofs'].keys() & erp_only['shop_scopes'].keys())
            try:
                sales = capture_sales_batch(settings, capture, candidates, erp_only['shop_scopes'],
                                            progress=progress, client=client, auth=auth)
            except SourceUnavailable:
                sales_status = 'unavailable'
            else:
                content = Path(sales['artifact_file']).read_bytes()
                sales = json.loads(content)
                sales_ref = {key:sales[key] for key in ('source_id', 'erp_capture_id', 'synthetic', 'sample_only',
                             'acquisition_complete', 'summary', 'artifact_file')}
                sales_ref['sha256'] = sha256(content).hexdigest()
                sales_status = 'saved' if sales['acquisition_complete'] else 'partial'
            order_preparation = prepare_order_losses(capture, native, sales, order_headers=headers)
            # 仅预取已定位的亏损目标；来源覆盖与缺项保留在最终预览中，
            # 预取成功本身不构成算价或提交授权。
            prepared = prepare_pricing(capture, order_preparation=order_preparation).report
            refs = prepared['references']
            for row_id in refs['retained_row_ids']:
                row = refs['lines'][row_id]
                if Decimal(row['loss_input']['amount']) >= 0:
                    continue
                pricing_targets.append({'erp_shop':row['erp_shop'],
                    'erp_sku':order_preparation['proofs'][row['order_id']]['erp_sku'],
                    'main_sku':row['main_sku'],'key':row['target']})
            official_history, pricing_targets, history_filter = filter_candidate_history(
                settings, pricing_targets, client=client, auth=auth)
            platform_targets = [{'erp_shop':t['erp_shop'],'product_id':t['key']['product_id'],
                'item_id':t['key']['item_id']} for t in pricing_targets]
            try:
                platform = capture_platform_controls(settings,capture,platform_targets,progress=progress,
                                                     client=client,auth=auth,
                                                     activity_hints=export_activity_hints(sales) if sales else None)
            except SourceUnavailable:
                platform_status = 'unavailable'
            else:
                content = Path(platform['artifact_file']).read_bytes()
                platform = json.loads(content)
                platform_ref = {key:platform[key] for key in ('source_id','erp_capture_id','synthetic','sample_only',
                    'acquisition_complete','artifact_file')}
                platform_ref['sha256'] = sha256(content).hexdigest()
                platform_status = 'saved' if platform['acquisition_complete'] else 'partial'
        fixed_fees = capture_current_fees(settings, capture, daily=daily_fees)
        fixed_fees_ref = {key:fixed_fees[key] for key in ('source_id','erp_capture_id','synthetic','sample_only',
            'acquisition_complete','artifact_file')}
        fixed_fees_ref['sha256'] = sha256(Path(fixed_fees['artifact_file']).read_bytes()).hexdigest()
        local_history = capture_local_price_history(settings,capture)
        content = Path(local_history['artifact_file']).read_bytes()
        local_history = json.loads(content)
        local_history_ref = {key:local_history[key] for key in ('source_id','erp_capture_id','synthetic','sample_only',
            'acquisition_complete','artifact_file')}
        local_history_ref['sha256'] = sha256(content).hexdigest()
        local_history_status = 'saved' if local_history['acquisition_complete'] else 'partial'
        if pricing_targets and all(source is not None for source in (sales,platform,fixed_fees)):
            # 参考不属于核心来源合同；其issues不合入在线准备或待处理清单。
            price_references = capture_sku_price_references(settings,capture,pricing_targets,
                platform=platform,progress=progress,client=client,auth=auth)
            online_preparation = prepare_online_states(capture,pricing_targets,sales=sales,
                platform=platform,local_history=local_history,fixed_fees=fixed_fees,
                official_history=official_history,price_references=price_references)
        try:
            current_fx = query_current_erp_fx(settings)
        except SourceUnavailable:
            # 公共汇率不可取时保留订单及缺项报告，不填默认汇率、不回退旧日配置。
            fx_status = 'unavailable'
        else:
            fx_path = Path(settings.runtime_dir) / 'source-fx' / (current_fx['ref'] + '.json')
            write_verified(fx_path, current_fx)
            current_fx = json.loads(fx_path.read_text(encoding='utf-8'))
            fx_status = 'saved'
    report = save_pricing_preview(settings.runtime_dir, capture_id, evidence, exports=exports,
                                  current_fx=current_fx, sales_source=sales_ref, native_source=native_ref,
                                  order_preparation=order_preparation, order_headers_source=headers_ref,
                                  local_history_source=local_history_ref, platform_source=platform_ref, online_preparation=online_preparation,fixed_fees_source=fixed_fees_ref)
    # 下游只消费已持久化并回读的报告，待提价保留完整 Decimal 文本和明细引用。
    report, digest = load_pricing_preview(settings.runtime_dir, capture_id, report['preview_id'])
    ready = [row for row in report['decisions'] if row['ready']]
    other = [row for row in report['decisions'] if not row['ready']]
    unresolved = bool(report['global_issues'] or report['issues'] or any(
        row['status'] == '待处理' for row in other
    ))
    status = 'needs_attention' if unresolved else 'completed'
    if unresolved and not ready:
        status = 'blocked'
    directory = Path(settings.runtime_dir) / 'erp-workflows' / uuid4().hex
    directory.mkdir(mode=0o700, parents=True)
    provenance = {
        'capture_id': capture_id, 'preview_id': report['preview_id'], 'preview_sha256': digest,
        'synthetic': report['synthetic'], 'execution_allowed': False,
    }
    if capture.get('department'):
        provenance['department'] = capture['department']
    write_verified(directory / 'pending-prices.json', {**provenance, 'decisions': ready})
    write_verified(directory / 'unresolved.json', {
        **provenance, 'global_issues': report['global_issues'],
        'issues': report['issues'], 'decisions': other,
    })
    result = {
        **provenance, 'status': status, 'mode': 'replay' if replay else 'capture',
        'capture_complete': capture['capture_complete'], 'cleaning_status': 'saved',
        'fx_status': fx_status,
        'sales_status': sales_status,
        'native_status': native_status,
        'order_headers_status': headers_status,
        'history_filter':history_filter,
        'platform_status': platform_status,
        'online_prepared_records':len(online_preparation['online']) if online_preparation else 0,
        'online_preparation_issues':len(online_preparation['issues']) if online_preparation else None,
        'local_history_status': local_history_status,
        'history_scope':'official_type_2_last_72_hours',
        'fixed_fee_status':('saved' if fixed_fees['acquisition_complete'] else 'unavailable') if fixed_fees else 'not_requested',
        'order_preparation_summary': order_preparation['summary'] if order_preparation is not None else None,
        'summary': report['summary'], 'ready_prices': len(ready),
        'issue_counts': dict(Counter(row['code'] for row in report['issues'])),
        'global_issues': report['global_issues'], 'artifact_dir': str(directory.resolve()),
    }
    if headers is not None and capture.get('department'):
        candidate_file = directory / 'seller-candidates.json'
        write_verified(candidate_file, {
            'capture_id': capture_id, 'department': capture['department'],
            'order_headers_source': headers_ref,
            **summarize_seller_candidates(capture, headers),
        })
        result['seller_candidates_file'] = str(candidate_file.resolve())
    write_verified(directory / 'result.json', result)
    return result
