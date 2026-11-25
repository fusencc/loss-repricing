"""四来源缺项时只放开证据预取，最终算价门禁仍生效。"""
import json
from types import SimpleNamespace

from app.services import erp_workflow as workflow


def test_rate_limited_partial_capture_prices_known_targets(tmp_path, monkeypatch):
    capture = {'capture_id': 'capture', 'department': 'D', 'capture_complete': False,
               'groups': [{'candidate_order_ids': ['order']}], 'rate_limit_observed': True}

    def artifact(name, **extra):
        data = {'source_id': name, 'erp_capture_id': 'capture', 'synthetic': True,
                'sample_only': False, 'acquisition_complete': True,
                'artifact_file': str(tmp_path / (name + '.json')), **extra}
        (tmp_path / (name + '.json')).write_text(json.dumps(data))
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
    monkeypatch.setattr(workflow, 'save_pricing_preview', lambda *a, **k: {'preview_id': 'preview'})
    final = {'preview_id': 'preview', 'synthetic': True, 'decisions': [{'ready': True}],
             'global_issues': ['候选名单未取全'], 'issues': [], 'summary': {}}
    monkeypatch.setattr(workflow, 'load_pricing_preview', lambda *a: (final, 'digest'))
    monkeypatch.setattr(workflow, 'summarize_seller_candidates', lambda *a: {})

    result = workflow._evaluate_capture(SimpleNamespace(runtime_dir=tmp_path), capture, replay=False,
                                        headers=headers, native=native, daily_fees={})
    assert seen == [{'erp_shop': 'shop', 'erp_sku': 'sku', 'main_sku': 'main', 'key': key}]
    assert result['status'] == 'needs_attention' and result['ready_prices'] == 1
    assert result['global_issues'] == ['候选名单未取全']
    saved = json.loads((tmp_path / 'erp-workflows' / result['artifact_dir'].split('/')[-1] / 'unresolved.json').read_text())
    assert saved['global_issues'] == ['候选名单未取全']
