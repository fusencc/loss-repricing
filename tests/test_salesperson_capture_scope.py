"""销售员采集的筛选与财务归属必须一致，不冒充四来源齐备。"""
import json
from pathlib import Path
from app.services.erp_pricing import preview_pricing


def scoped_capture():
    capture=json.loads((Path(__file__).parent/'fixtures/erp-pricing-capture.sample.json').read_text())
    capture['groups']=[g for g in capture['groups'] if g['source'].startswith('loss_')]
    capture['salesperson']={'name':'测试销售员','id':'USER-1'}
    for g in capture['groups']:
        g['filters']['username']='测试销售员'
        for row in g['rows']:row['fields']['销售员']='测试销售员'
    return capture


def test_verified_salesperson_selector_is_a_valid_scope_not_full_four_sources():
    report=preview_pricing(scoped_capture())
    assert report['consistent_capture_scope']
    assert next(r for r in report['source_coverage'] if r['source']=='loss_allocatable')['finance_complete']
    assert '采集来源的筛选范围不一致或无法确认' not in report['global_issues']
    assert any('四类' in issue for issue in report['global_issues'])


def test_mixed_salesperson_selectors_are_not_a_single_scope():
    capture=scoped_capture();capture['groups'][1]['filters']['username']='其他销售员'
    assert not preview_pricing(capture)['consistent_capture_scope']


def test_filtered_capture_requires_verified_salesperson_binding():
    capture=scoped_capture();capture.pop('salesperson')
    assert not preview_pricing(capture)['consistent_capture_scope']


def test_finance_row_cannot_belong_to_a_different_salesperson():
    capture=scoped_capture();g=capture['groups'][0];g['rows'][0]['fields']['销售员']='其他销售员'
    report=preview_pricing(capture)
    assert not next(r for r in report['source_coverage'] if r['source']==g['source'])['finance_complete']


def test_unknown_extra_filter_is_still_rejected():
    capture=scoped_capture();capture['groups'][0]['filters']['unreviewedFilter']='x'
    assert not preview_pricing(capture)['consistent_capture_scope']
