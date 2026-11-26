"""四来源缺项时只放开证据预取，最终算价门禁仍生效。"""
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from app.services import erp_workflow as workflow


def test_rate_limited_partial_capture_prices_known_targets(tmp_path, monkeypatch):
    capture = {'capture_id': 'capture', 'department': 'D', 'capture_complete': False,
               'groups': [{'candidate_order_ids': ['order']}], 'rate_limit_observed': True}

    def artifact(name, **extra):
        data = {'source_id': name, 'erp_capture_id': 'capture', 'synthetic': True,
                'sample_only': False, 'acquisition_complete': True,
                'artifact_file': str(tmp_path / (name + '.json')), **extra}
        return data

    headers = artifact('headers')
    native = artifact('native', summary={})
    sales = artifact('sales', summary={})
    platform = artifact('platform')
    fees = artifact('fees')
    history = artifact('local')
    monkeypatch.setattr(workflow, 'capture_sales_batch', lambda *a, **k: sales)
    monkeypatch.setattr(workflow, 'capture_platform_controls', lambda *a, **k: platform)
    monkeypatch.setattr(workflow, 'capture_current_fees', lambda *a, **k: fees)
    monkeypatch.setattr(workflow, 'capture_local_price_history', lambda *a, **k: history)
    monkeypatch.setattr(workflow, 'select_export_candidates', lambda *a: [])
    monkeypatch.setattr(workflow, 'export_activity_hints', lambda *a: {})
    order = {'proofs': {'order': {'erp_sku': 'sku'}}, 'shop_scopes': {'order': {}}, 'summary': {}}
    monkeypatch.setattr(workflow, 'prepare_order_losses', lambda *a, **k: order)
    key = {'shop_id': 'shop', 'product_id': 'product', 'item_id': 'item'}
    report = {'evaluation_complete_sources': ['loss_allocatable'], 'references': {
        'retained_row_ids': ['row'], 'lines': {'row': {'loss_input': {'amount': '-1'},
        'erp_shop': 'shop', 'order_id': 'order', 'main_sku': 'main', 'target': key}}}}
    monkeypatch.setattr(workflow, 'prepare_pricing', lambda *a, **k: SimpleNamespace(report=report))
    seen = []
    def filter_history(settings, targets, **kwargs):
        seen.extend(targets)
        return None, targets, []
    monkeypatch.setattr(workflow, 'filter_candidate_history', filter_history)
    monkeypatch.setattr(workflow, 'capture_sku_price_references', lambda *a, **k: {})
    monkeypatch.setattr(workflow, 'prepare_online_states', lambda *a, **k: {'online': [], 'issues': []})
    final = {'preview_id': 'preview', 'synthetic': True, 'decisions': [{'ready': True}],
             'global_issues': ['候选名单未取全'], 'issues': [], 'summary': {}}
    monkeypatch.setattr(workflow, 'save_capture_preview', lambda *a, **k: (final, 'digest'))
    monkeypatch.setattr(workflow, 'summarize_seller_candidates', lambda *a: {})

    result = workflow._evaluate_capture(SimpleNamespace(runtime_dir=tmp_path), capture, replay=False,
                                        headers=headers, native=native, daily_fees={})
    assert seen == [{'erp_shop': 'shop', 'erp_sku': 'sku', 'main_sku': 'main', 'key': key}]
    assert result['status'] == 'needs_attention' and result['ready_prices'] == 1
    assert result['global_issues'] == ['候选名单未取全']
    from app.services.erp_capture_store import write_json

    # 来源文件在评估时不存在。用实际写入格式独立核对返回的归档引用。
    write_json(headers['artifact_file'], headers)
    seller_report = json.loads(Path(result['seller_candidates_file']).read_text())
    assert seller_report['order_headers_source']['sha256'] == sha256(Path(headers['artifact_file']).read_bytes()).hexdigest()
    saved = json.loads((Path(result['artifact_dir']) / 'unresolved.json').read_text())
    assert saved['global_issues'] == ['候选名单未取全']


def test_in_memory_evaluation_archives_a_replayable_preview_without_intermediate_reads(tmp_path, monkeypatch):
    from app.services.erp_pricing import load_pricing_preview

    fixtures = Path(__file__).parent / 'fixtures'
    capture = json.loads((fixtures / 'erp-pricing-capture.sample.json').read_text())
    evidence = json.loads((fixtures / 'erp-pricing-evidence.sample.json').read_text())
    path_open = Path.open

    def writes_only(path, mode='r', *args, **kwargs):
        if path.is_relative_to(tmp_path):
            assert 'r' not in mode, f'Unexpected intermediate read: {path.name}'
        return path_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', writes_only)
        result = workflow._evaluate_capture(SimpleNamespace(runtime_dir=tmp_path), capture, replay=True, evidence=evidence)

    report, digest = load_pricing_preview(tmp_path, capture['capture_id'], result['preview_id'])
    preview = tmp_path / 'erp-pricing-previews' / (result['preview_id'] + '.json')
    assert result['preview_sha256'] == digest == sha256(preview.read_bytes()).hexdigest()
    assert report['batch_input_available'] and result['ready_prices'] == 2
    pending = json.loads((Path(result['artifact_dir']) / 'pending-prices.json').read_text())
    assert pending['decisions'] == [row for row in report['decisions'] if row['ready']]
