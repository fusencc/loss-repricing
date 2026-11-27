"""ERP 候选至待提价文件的只读编排；复用统一规则，不调用模型或价格上传。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from pathlib import Path
from uuid import uuid4
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
from app.adapters.sales_batch_export import capture_sales_batch, select_export_candidates
from app.adapters.sales_batch_controls import capture_batch_controls
from app.adapters.live_online_preparation import prepare_online_states
from app.services.erp_capture_store import (
    json_artifact_bytes, load_capture, rebuild_clean_capture, save_capture, write_json, write_verified,
)
from app.services.erp_department_capture import department_captures
from app.services.erp_pricing import save_capture_preview
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


def run_workflow(settings, *, capture_id=None, evidence=None, exports=None, progress=None, on_department=None,
                 departments_to_collect=None):
    """先采一次集团四来源，再按财务表部门批查订单；指定采集编号时回放。"""
    if departments_to_collect is None:
        departments_to_collect = ERP_DEPARTMENTS
    if capture_id is not None:
        rebuild_clean_capture(settings.runtime_dir, capture_id)
        capture = load_capture(settings.runtime_dir, capture_id)
        return _evaluate_capture(settings, capture, replay=True, evidence=evidence, exports=exports, progress=progress)
    if evidence is not None or exports is not None:
        raise ValueError('补充文件只可用于明确指定采集编号的离线对账')

    directory = Path(settings.runtime_dir) / 'erp-workflows' / uuid4().hex
    directory.mkdir(mode=0o700, parents=True)
    departments = []
    summary = {
        'mode': 'department_capture', 'status': 'running', 'execution_allowed': False,
        'department_order': list(departments_to_collect), 'departments': departments,
        'ready_prices': 0, 'artifact_dir': str(directory.resolve()),
    }
    manifest = directory / 'result.json'
    write_verified(manifest, summary)

    with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
        auth = SalesAuth(settings, client)
        try:
            daily_fees = refresh_daily_fees(settings, client=client, auth=auth)
        except SourceUnavailable as error:
            event('erp.failed', exception=error, source='erp', stage='collect', error_code='source_unavailable')
            daily_fees = None
        current_fx = _capture_round_fx(settings)
        with _session(settings) as web_session:
            if progress:
                progress('阶段 1/2：按集团采集四类名单和订单财务表。')
            try:
                parent = save_capture(settings.runtime_dir, capture_erp_bulk(settings, progress=progress,
                                                                            web_session=web_session))
                summary['group_capture_id'] = parent['capture_id']
                scoped, unassigned = department_captures(parent, ERP_DEPARTMENTS)
                summary['unassigned_orders'] = unassigned
            except Exception as error:
                summary.update(_workflow_failure(error))
                write_json(manifest, summary)
                return summary
            if progress:
                progress('阶段 2/2：按财务表部门分组，批量读取订单店铺归属和原生商品明细。')
            pending = {}
            for department in departments_to_collect:
                capture = None
                result = {'department': department, 'status': 'collecting', 'ready_prices': 0}
                departments.append(result)
                try:
                    capture = save_capture(settings.runtime_dir, scoped.pop(department))
                    if progress:
                        progress(f'{department}：按本轮已导出的订单号批查。')
                    headers, native = _capture_erp_sources(settings, capture, progress, web_session)
                    pending[department] = capture, headers, native
                    result.update(capture_id=capture['capture_id'], status='collected')
                except Exception as error:
                    result.update(_workflow_failure(error, capture))
                write_json(manifest, summary)

        def evaluate_department(result):
            department=result['department']
            capture,headers,native=pending[department]
            with httpx.Client(timeout=60,trust_env=False,follow_redirects=False) as department_client:
                department_auth=SalesAuth(settings,department_client)
                try:
                    return _evaluate_capture(settings,capture,replay=False,progress=progress,
                        headers=headers,native=native,daily_fees=daily_fees,current_fx=current_fx,
                        client=department_client,auth=department_auth)
                except Exception as error:
                    return _workflow_failure(error,capture)

        with ThreadPoolExecutor(max_workers=len(ERP_DEPARTMENTS)) as pool:
            ready = [result for result in departments if result['department'] in pending]
            for result in ready:
                result['status'] = 'processing'
            if ready:
                write_json(manifest, summary)
            futures = {pool.submit(evaluate_department, result): result for result in ready}
            for future in as_completed(futures):
                result=futures[future]
                result.update(future.result())
                summary['ready_prices']+=result['ready_prices']
                write_json(manifest,summary)
                if on_department:on_department(result)
                if progress:progress(f"{result['department']} 处理结果：{result['status']}。")

    if parent['capture_complete'] and not unassigned and all(d['status'] == 'completed' for d in departments):
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
              'rate_limit_observed': isinstance(error, ErpAccountRateLimited)}
    if capture is not None:
        result.update({key: capture[key] for key in ('capture_id', 'capture_complete', 'cleaning_status')})
        result['rate_limit_observed'] |= capture.get('rate_limit_observed', False)
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
    return not any(group['candidate_order_ids'] for group in capture['groups'])


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
        return {
            'capture_id': capture_id, 'department': capture['department'], 'mode': 'capture',
            'status': 'completed' if capture['capture_complete'] else 'needs_attention',
            'capture_complete': capture['capture_complete'], 'cleaning_status': capture.get('cleaning_status', 'saved'),
            'ready_prices': 0, 'execution_allowed': False,
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

    def source_ref(source, *extra):
        fields = ('source_id', 'erp_capture_id', 'synthetic', 'sample_only', 'acquisition_complete',
                  'artifact_file', *extra)
        return {**{key: source[key] for key in fields}, 'sha256': sha256(json_artifact_bytes(source)).hexdigest()}

    if not replay:
        headers_ref = source_ref(headers)
        headers_status = 'saved' if headers['acquisition_complete'] else 'partial'
        if native is None:
            native_status = 'unavailable'
        else:
            native_ref = source_ref(native, 'summary')
            native_status = 'saved' if native['acquisition_complete'] else 'partial'
            # 先核对同订单的财务、明细和店铺，再按负修正一次毛利圈定导出范围。
            erp_only = prepare_order_losses(capture, native, order_headers=headers)
            candidates = select_export_candidates(capture, erp_only['proofs'].keys() & erp_only['shop_scopes'].keys())
            try:
                sales = capture_sales_batch(settings, capture, candidates, erp_only['shop_scopes'],
                                            progress=progress, client=client, auth=auth)
            except SourceUnavailable:
                sales_status = 'unavailable'
                order_preparation = erp_only
            else:
                sales_ref = source_ref(sales, 'summary')
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
                platform = capture_batch_controls(settings,capture,platform_targets,progress=progress,
                                                     client=client,auth=auth,
                                                     sales=sales)
            except SourceUnavailable:
                platform_status = 'unavailable'
            else:
                platform_ref = source_ref(platform)
                platform_status = 'saved' if platform['acquisition_complete'] else 'partial'
        if daily_fees is not None:
            fixed_fees = capture_current_fees(settings, capture, daily=daily_fees)
            fixed_fees_ref = source_ref(fixed_fees)
        local_history = capture_local_price_history(settings,capture)
        local_history_ref = source_ref(local_history)
        local_history_status = 'saved' if local_history['acquisition_complete'] else 'partial'
        if pricing_targets and all(source is not None for source in (sales,platform,fixed_fees)):
            # 参考不属于核心来源合同；其issues不合入在线准备或待处理清单。
            price_references = capture_sku_price_references(settings,capture,pricing_targets,
                platform=platform,progress=progress,client=client,auth=auth)
            online_preparation = prepare_online_states(capture,pricing_targets,sales=sales,
                platform=platform,local_history=local_history,fixed_fees=fixed_fees,
                official_history=official_history,price_references=price_references)
    report, digest = save_capture_preview(settings.runtime_dir, capture, evidence, exports=exports,
        current_fx=current_fx, sales_source=sales_ref, native_source=native_ref,
        order_preparation=order_preparation, order_headers_source=headers_ref,
        local_history_source=local_history_ref, platform_source=platform_ref,
        online_preparation=online_preparation, fixed_fees_source=fixed_fees_ref, official_history=official_history)
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
    write_json(directory / 'pending-prices.json', {**provenance, 'decisions': ready})
    write_json(directory / 'unresolved.json', {
        **provenance, 'global_issues': report['global_issues'],
        'issues': report['issues'], 'decisions': other,
    })
    result = {
        **provenance, 'status': status, 'mode': 'replay' if replay else 'capture',
        'capture_complete': capture['capture_complete'], 'cleaning_status': capture.get('cleaning_status', 'saved'),
        'rate_limit_observed': any(src and src.get('rate_limit_observed') for src in (capture, headers, native)),
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
        write_json(candidate_file, {
            'capture_id': capture_id, 'department': capture['department'],
            'order_headers_source': headers_ref,
            **summarize_seller_candidates(capture, headers),
        })
        result['seller_candidates_file'] = str(candidate_file.resolve())
    write_json(directory / 'result.json', result)
    return result
