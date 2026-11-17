"""ERP 候选至待提价文件的只读编排；复用统一规则，不调用模型或价格上传。"""
from collections import Counter
from pathlib import Path
from uuid import uuid4
import json
from hashlib import sha256

from app.adapters.base import SourceUnavailable
from app.adapters.normalization import source_time
from app.adapters.erp_bulk_capture import capture_erp_bulk
from app.adapters.erp_rates import query_current_erp_fx
from app.adapters.erp_detail_capture import capture_erp_details
from app.adapters.erp_order_loss import prepare_order_losses
from app.adapters.erp_order_headers import capture_order_headers
from app.adapters.sales_sales_window import capture_sales_window
from app.adapters.fixed_pricing_fees import capture_fixed_fees
from app.adapters.erp_pricing import prepare_pricing
from app.services.local_price_history import capture_local_price_history
from decimal import Decimal
from app.adapters.sales_capture import capture_sales_sources
from app.adapters.sales_platform_capture import capture_platform_controls
from app.adapters.live_online_preparation import prepare_online_states
from app.services.erp_capture_store import load_capture, rebuild_clean_capture, save_capture, write_verified
from app.services.erp_pricing import load_pricing_preview, save_pricing_preview


def run_workflow(settings, *, capture_id=None, evidence=None, exports=None, progress=None):
    """默认实时抓四类；显式 capture_id 才离线回放，绝不自动回退旧归档。

    evidence 须绑定已有采集号，金额粒度和币种沿用既有证据合同。
    当前 ERP 财务导出没有逐明细亏损及在线状态，缺证据时归档原因并报阻塞。
    """
    replay = capture_id is not None
    if not replay:
        if evidence is not None or exports is not None:
            raise ValueError('补充文件只可用于明确指定采集编号的离线对账')
        collected = capture_erp_bulk(settings, progress=progress)
        saved = save_capture(settings.runtime_dir, collected)
        capture_id = saved['capture_id']
        # 原件成功但清洗失败时不得让后续空文件看起来像已完成。
        if saved['cleaning_status'] != 'saved':
            raise OSError('原件已存档，清洗失败；请按采集编号重建清洗')
    else:
        rebuild_clean_capture(settings.runtime_dir, capture_id)

    capture = load_capture(settings.runtime_dir, capture_id)
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
    native_status = 'not_requested'
    sales_status = 'not_requested'
    fx_status = 'file_evidence' if evidence is not None else 'not_requested'
    if not replay:
        try:
            sales = capture_sales_sources(settings, capture, progress=progress)
        except SourceUnavailable:
            sales_status = 'unavailable'
        else:
            sales_path = Path(sales['artifact_file'])
            content = sales_path.read_bytes()
            sales = json.loads(content)
            sales_ref = {key:sales[key] for key in ('source_id', 'erp_capture_id', 'synthetic', 'sample_only',
                         'acquisition_complete', 'summary', 'artifact_file')}
            sales_ref['sha256'] = sha256(content).hexdigest()
            sales_status = 'saved' if sales['acquisition_complete'] else 'partial'
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
            order_preparation = prepare_order_losses(capture, native, sales, order_headers=headers)
            # 完整性、归属冲突及整店挂起仍由同一准备器判定，再抓可能算价对象的七日窗口。
            prepared = prepare_pricing(capture, order_preparation=order_preparation).report
            plans = {}
            platform_targets = []
            if len(prepared['evaluation_complete_sources']) == 4:
                refs = prepared['references']
                for row_id in refs['retained_row_ids']:
                    row = refs['lines'][row_id]
                    if Decimal(row['loss_input']['amount']) >= 0:
                        continue
                    target = {'erp_shop':row['erp_shop'],'shop_id':row['target']['shop_id'],
                        'erp_sku':order_preparation['proofs'][row['order_id']]['erp_sku'],'main_sku':row['main_sku']}
                    plans[tuple(target.values())] = target
                    pricing_targets.append({'erp_shop':row['erp_shop'],'erp_sku':target['erp_sku'],
                        'main_sku':row['main_sku'],'key':row['target']})
                    platform_targets.append({'erp_shop':row['erp_shop'],'product_id':row['target']['product_id'],
                        'item_id':row['target']['item_id']})
            try:
                window = capture_sales_window(settings,capture,list(plans.values()),progress=progress)
            except SourceUnavailable:
                window_status = 'unavailable'
            else:
                content = Path(window['artifact_file']).read_bytes()
                window = json.loads(content)
                window_ref = {key:window[key] for key in ('source_id','erp_capture_id','synthetic','sample_only',
                    'acquisition_complete','artifact_file','window_start','window_end')}
                window_ref['sha256'] = sha256(content).hexdigest()
                window_status = 'saved' if window['acquisition_complete'] else 'partial'
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
        fixed_fees = capture_fixed_fees(settings,capture)
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
        if all(source is not None for source in (sales,platform,window,fixed_fees)):
            online_preparation = prepare_online_states(capture,pricing_targets,sales=sales,
                platform=platform,window=window,local_history=local_history,fixed_fees=fixed_fees)
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
        'platform_status': platform_status,
        'online_prepared_records':len(online_preparation['online']) if online_preparation else 0,
        'online_preparation_issues':len(online_preparation['issues']) if online_preparation else None,
        'local_history_status': local_history_status,
        'history_scope':'own_program_only',
        'fixed_fee_status':('saved' if fixed_fees['acquisition_complete'] else 'unavailable') if fixed_fees else 'not_requested',
        'order_preparation_summary': order_preparation['summary'] if order_preparation is not None else None,
        'summary': report['summary'], 'ready_prices': len(ready),
        'issue_counts': dict(Counter(row['code'] for row in report['issues'])),
        'global_issues': report['global_issues'], 'artifact_dir': str(directory.resolve()),
    }
    write_verified(directory / 'result.json', result)
    return result
