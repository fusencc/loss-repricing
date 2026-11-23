"""ERP 候选至待提价文件的只读编排；复用统一规则，不调用模型或价格上传。"""
from collections import Counter
from pathlib import Path
from uuid import uuid4
import json
from hashlib import sha256
from time import sleep

from app.adapters.base import SourceUnavailable
from app.adapters.normalization import source_time
from app.adapters.erp_bulk_capture import ERP_DEPARTMENTS, capture_erp_bulk
from app.adapters.erp_web import ErpAccountRateLimited
from app.adapters.erp_rates import query_current_erp_fx
from app.adapters.erp_detail_capture import capture_erp_details
from app.adapters.erp_order_loss import prepare_order_losses
from app.adapters.erp_order_headers import capture_order_headers
from app.adapters.erp_goods_sales import capture_goods_sales
from app.adapters.current_pricing_fees import capture_current_fees, refresh_daily_fees
from app.adapters.sales_price_reference import capture_sku_price_references
from app.adapters.erp_pricing import prepare_pricing
from app.services.local_price_history import capture_local_price_history
from app.adapters.official_price_logs import capture_recent_price_changes, recent_price_log_for
from app.domain.types import VariantKey
from app.observability import event
from decimal import Decimal
from app.adapters.sales_batch_export import capture_sales_batch, select_export_candidates
from app.adapters.sales_platform_capture import capture_platform_controls
from app.adapters.live_online_preparation import prepare_online_states
from app.services.erp_capture_store import load_capture, rebuild_clean_capture, save_capture, write_verified
from app.services.erp_pricing import load_pricing_preview, save_pricing_preview
from app.services.erp_seller_candidates import summarize_seller_candidates

def capture_candidate_references(settings, capture, targets, *, progress=None):
    """先执行必要的官方历史保护，再补可缺失的销量参考；无全店查询。"""
    history = (capture_recent_price_changes(settings,[VariantKey.model_validate(t['key']) for t in targets])
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
    reference=capture_goods_sales(settings,capture,eligible,progress=progress)
    return history,eligible,reference,filtered


def run_workflow(settings, *, capture_id=None, evidence=None, exports=None, progress=None):
    """按部门串行完成只读采集和算价，显式指定采集编号时回放单批。"""
    if capture_id is not None:
        return _run_workflow(settings, capture_id=capture_id, evidence=evidence, exports=exports, progress=progress)
    if evidence is not None or exports is not None:
        raise ValueError('补充文件只可用于明确指定采集编号的离线对账')

    directory = Path(settings.runtime_dir) / 'erp-workflows' / uuid4().hex
    directory.mkdir(mode=0o700, parents=True)
    departments = []
    summary = {
        'mode': 'department_capture', 'status': 'running', 'execution_allowed': False,
        'department_order': list(ERP_DEPARTMENTS), 'departments': departments,
        'ready_prices': 0, 'artifact_dir': str(directory.resolve()),
    }
    manifest = directory / 'result.json'
    write_verified(manifest, summary)
    for department in ERP_DEPARTMENTS:
        if departments and departments[-1].get('stopped_by_rate_limit'):
            # 新部门开始自己的任务，仍遵守同一 ERP 账号的请求间隔。
            sleep(settings.erp_capture_request_interval_seconds)
        if progress:
            progress(f'开始处理 {department}。')
        try:
            result = _run_workflow(settings, department=department, progress=progress)
        except Exception as error:
            # 单部门异常在调度边界留痕，不能终止其他独立部门。
            result = _workflow_failure(error)
        departments.append({**result, 'department': department})
        summary['ready_prices'] += result['ready_prices']
        write_verified(manifest, summary)
        if progress:
            progress(f"{department} 处理结果：{result['status']}。")
    if all(department['status'] == 'completed' for department in departments):
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


def _run_workflow(settings, *, department=None, capture_id=None, evidence=None, exports=None, progress=None):
    if capture_id is not None:
        rebuild_clean_capture(settings.runtime_dir, capture_id)
        return _evaluate_capture(settings, capture_id, replay=True, evidence=evidence, exports=exports, progress=progress)

    # 每日任务启动前统一检查费用；各部门共用持久缓存，不逐批重查。
    refresh_daily_fees(settings)
    collected = capture_erp_bulk(settings, department=department, progress=progress)
    saved = save_capture(settings.runtime_dir, collected)
    capture_id = saved['capture_id']
    try:
        # 原件成功但清洗失败时不得让后续空文件看起来像已完成。
        if saved['cleaning_status'] != 'saved':
            raise OSError('原件已存档，清洗失败；请按采集编号重建清洗')
        return _evaluate_capture(settings, capture_id, replay=False, progress=progress)
    except Exception as error:
        # 算价或清洗失败时，仍保留已经写入的采集编号供核对。
        return {**_workflow_failure(error), 'capture_id': capture_id, 'department': department,
                'capture_complete': saved['capture_complete'], 'cleaning_status': saved['cleaning_status'],
                'stopped_by_rate_limit': saved.get('stopped_by_rate_limit', False) or isinstance(error, ErpAccountRateLimited)}


def _evaluate_capture(settings, capture_id, *, replay, evidence=None, exports=None, progress=None):
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
    window_ref = None
    window_status = 'not_requested'
    platform_ref = None
    platform_status = 'not_requested'
    local_history_ref = None
    local_history_status = 'not_requested'
    fixed_fees = None
    fixed_fees_ref = None
    order_preparation = None
    online_preparation = None
    window = None
    platform = None
    pricing_targets = []
    history_filter = []
    official_history = None
    native_status = 'not_requested'
    sales_status = 'not_requested'
    fx_status = 'file_evidence' if evidence is not None else 'not_requested'
    if not replay:
        headers = capture_order_headers(settings, capture, progress=progress)
        content = Path(headers['artifact_file']).read_bytes()
        headers = json.loads(content)
        headers_ref = {key:headers[key] for key in ('source_id','erp_capture_id','synthetic','sample_only',
            'acquisition_complete','artifact_file')}
        headers_ref['sha256'] = sha256(content).hexdigest()
        headers_status = 'saved' if headers['acquisition_complete'] else 'partial'
        try:
            native = capture_erp_details(settings, capture, progress=progress)
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
                sales = capture_sales_batch(settings, capture, candidates, erp_only['shop_scopes'], progress=progress)
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
            # 完整性、归属冲突及整店挂起仍由同一准备器判定，再抓可能算价对象的七日窗口。
            prepared = prepare_pricing(capture, order_preparation=order_preparation).report
            if len(prepared['evaluation_complete_sources']) == 4:
                refs = prepared['references']
                for row_id in refs['retained_row_ids']:
                    row = refs['lines'][row_id]
                    if Decimal(row['loss_input']['amount']) >= 0:
                        continue
                    target = {'erp_shop':row['erp_shop'],'shop_id':row['target']['shop_id'],
                        'erp_sku':order_preparation['proofs'][row['order_id']]['erp_sku'],'main_sku':row['main_sku']}
                    pricing_targets.append({'erp_shop':row['erp_shop'],'erp_sku':target['erp_sku'],
                        'main_sku':row['main_sku'],'key':row['target']})
            official_history, pricing_targets, window, history_filter = capture_candidate_references(
                settings,capture,pricing_targets,progress=progress)
            platform_targets = [{'erp_shop':t['erp_shop'],'product_id':t['key']['product_id'],
                'item_id':t['key']['item_id']} for t in pricing_targets]
            content = Path(window['artifact_file']).read_bytes()
            window = json.loads(content)
            window_ref = {key:window[key] for key in ('source_id','erp_capture_id','synthetic','sample_only',
                'acquisition_complete','artifact_file','window_start','window_end')}
            window_ref['sha256'] = sha256(content).hexdigest()
            window_status = 'saved' if window['acquisition_complete'] else 'partial_reference'
            try:
                platform = capture_platform_controls(settings,capture,platform_targets,progress=progress)
            except SourceUnavailable:
                platform_status = 'unavailable'
            else:
                content = Path(platform['artifact_file']).read_bytes()
                platform = json.loads(content)
                platform_ref = {key:platform[key] for key in ('source_id','erp_capture_id','synthetic','sample_only',
                    'acquisition_complete','artifact_file')}
                platform_ref['sha256'] = sha256(content).hexdigest()
                platform_status = 'saved' if platform['acquisition_complete'] else 'partial'
        fixed_fees = capture_current_fees(settings,capture,platform)
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
        if pricing_targets and all(source is not None for source in (sales,platform,window,fixed_fees)):
            # 参考不属于核心来源合同；其issues不合入在线准备或待处理清单。
            price_references = capture_sku_price_references(settings,capture,pricing_targets,
                platform=platform,progress=progress)
            online_preparation = prepare_online_states(capture,pricing_targets,sales=sales,
                platform=platform,window=window,local_history=local_history,fixed_fees=fixed_fees,
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
                                  sales_window_source=window_ref, local_history_source=local_history_ref, platform_source=platform_ref, online_preparation=online_preparation,fixed_fees_source=fixed_fees_ref)
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
        'sales_window_status': window_status,
        'sales_policy':'optional_official_warehouse_reference',
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
