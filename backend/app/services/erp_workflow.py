"""ERP 候选至待提价文件的只读编排；复用统一规则，不调用模型或价格上传。"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import date, datetime
from app.services.store import BEIJING
from pathlib import Path
from queue import Queue
from uuid import uuid4
import json
from hashlib import sha256
import httpx

from app.adapters.base import SourceUnavailable
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
    """名单按 ERP 采集与销售算价两条车道处理；指定采集编号时回放单批。"""
    if roster_person is not None and (roster_date is None or roster_person not in ERP_ROSTER_DEPARTMENTS):
        raise ValueError('名单刷新须绑定单日名单中的销售员')
    if capture_id is not None:
        if roster_date is not None:
            raise ValueError('名单采集不能与离线回放同时指定')
        rebuild_clean_capture(settings.runtime_dir, capture_id)
        capture = load_capture(settings.runtime_dir, capture_id)
        return _evaluate_capture(settings, capture, replay=True, evidence=evidence, exports=exports, progress=progress)
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

    def collect(department, name, web_session):
        capture = None
        try:
            if progress:
                progress(f'开始处理 {department}。')
            collected = capture_erp_bulk(settings, department=department, salesperson=name,
                                         progress=progress, web_session=web_session)
            capture = save_capture(settings.runtime_dir, collected)
            if capture['cleaning_status'] != 'saved':
                raise OSError('原件已存档，清洗失败；请按采集编号重建清洗')
            headers, native = _capture_erp_sources(settings, capture, progress, web_session)
            return department, name, capture, headers, native, None
        except Exception as error:
            return department, name, capture, None, None, _workflow_failure(error, capture)

    def consume(pool=None):
        with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
            auth = SalesAuth(settings, client)
            try:
                daily_fees = refresh_daily_fees(settings, client=client, auth=auth)
            except SourceUnavailable as error:
                event('erp.failed', exception=error, source='erp', stage='collect', error_code='source_unavailable')
                daily_fees = None
            current_fx = _capture_round_fx(settings)

            def finish(department, name, capture, headers, native, failure):
                scope = {'department': department, **({'salesperson': name} if name is not None else {})}
                result = failure or {}
                try:
                    if failure is None:
                        result = _evaluate_capture(settings, capture, replay=False, progress=progress,
                            headers=headers, native=native, daily_fees=daily_fees, current_fx=current_fx,
                            client=client, auth=auth)
                    result.update(scope)
                    if name is not None and on_roster_scope is not None:
                        on_roster_scope(result)
                    if progress:
                        progress(f"{department} 处理结果：{result['status']}。")
                except Exception as error:
                    result.update(_workflow_failure(error, capture))
                    result.update(scope)
                departments.append(result)
                summary['ready_prices'] += result['ready_prices']
                write_verified(manifest, summary)
                return result

            if pool is None:
                with _session(settings) as web_session:
                    for department, name in scopes:
                        if finish(*collect(department, name, web_session)).get('stopped_by_rate_limit'):
                            break
                return

            pending = Queue()

            def produce():
                try:
                    with _session(settings) as web_session:
                        for department, name in scopes:
                            item = collect(department, name, web_session)
                            pending.put(item)
                            if ((item[2] and item[2].get('stopped_by_rate_limit'))
                                    or (item[5] and item[5].get('stopped_by_rate_limit'))):
                                break
                finally:
                    pending.put(None)

            producer = pool.submit(copy_context().run, produce)
            while (task := pending.get()) is not None:
                finish(*task)
            producer.result()

    if names is None:
        consume()
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(copy_context().run, consume, pool).result()
    if len(departments) == len(scopes) and all(department['status'] == 'completed' for department in departments):
        summary['status'] = 'completed'
    elif summary['ready_prices'] or any(department['status'] == 'completed' for department in departments):
        summary['status'] = 'needs_attention'
    else:
        summary['status'] = 'blocked'
    write_verified(manifest, summary)
    return summary


def _workflow_failure(error, capture=None):
    error_id = event('erp.failed', exception=error, source='erp', synthetic=False,
                     stage='collect', error_code='workflow_failed')
    result = {'status': 'failed', 'error_id': error_id, 'ready_prices': 0, 'execution_allowed': False,
              'stopped_by_rate_limit': isinstance(error, ErpAccountRateLimited)}
    if capture is not None:
        result.update({key: capture[key] for key in ('capture_id', 'capture_complete', 'cleaning_status')})
        result['stopped_by_rate_limit'] |= capture.get('stopped_by_rate_limit', False)
    return result


def _capture_round_fx(settings):
    try:
        current_fx = query_current_erp_fx(settings)
    except SourceUnavailable:
        return None
    fx_path = Path(settings.runtime_dir) / 'source-fx' / (current_fx['ref'] + '.json')
    write_verified(fx_path, current_fx)
    return current_fx


def _capture_shortcut(capture):
    limited = capture.get('stopped_by_rate_limit', False)
    empty = capture['capture_complete'] and not any(group['candidate_order_ids'] for group in capture['groups'])
    return limited or empty


def _capture_erp_sources(settings, capture, progress, web_session):
    if _capture_shortcut(capture):
        return None, None
    headers = capture_order_headers(settings, capture, progress=progress, web_session=web_session)
    try:
        native = capture_erp_details(settings, capture, progress=progress, web_session=web_session)
    except SourceUnavailable:
        native = None
    return headers, native


def _evaluate_capture(settings, capture, *, replay, evidence=None, exports=None, progress=None, daily_fees=None,
                      current_fx=None, headers=None, native=None, client=None, auth=None):
    capture_id = capture['capture_id']
    if not replay and _capture_shortcut(capture):
        limited = capture.get('stopped_by_rate_limit', False)
        # 账号限流时停止本部门的后续取证。完整空名单已经确认无需进一步取数。
        return {
            'capture_id': capture_id, 'department': capture['department'], 'mode': 'capture',
            'status': 'blocked' if limited else 'completed',
            'capture_complete': capture['capture_complete'], 'cleaning_status': 'saved',
            'stopped_by_rate_limit': limited, 'ready_prices': 0, 'execution_allowed': False,
        }
    sales_ref = None
    sales = None
    native_ref = None
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
    fx_status = ('file_evidence' if evidence is not None else 'not_requested') if replay else (
        'saved' if current_fx is not None else 'unavailable')
    if not replay:
        headers_ref = {key:headers[key] for key in ('source_id','erp_capture_id','synthetic','sample_only',
            'acquisition_complete','artifact_file')}
        headers_ref['sha256'] = sha256(Path(headers['artifact_file']).read_bytes()).hexdigest()
        headers_status = 'saved' if headers['acquisition_complete'] else 'partial'
        if native is None:
            native_status = 'unavailable'
        else:
            native_ref = {key:native[key] for key in ('source_id','erp_capture_id','synthetic','sample_only',
                          'acquisition_complete','summary','artifact_file')}
            native_ref['sha256'] = sha256(Path(native['artifact_file']).read_bytes()).hexdigest()
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
        if daily_fees is not None:
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
