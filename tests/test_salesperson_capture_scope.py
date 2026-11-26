"""按销售员核对各来源的筛选、候选和财务归属。"""
from contextlib import nullcontext
import json
from pathlib import Path

import pytest

from app.adapters import erp_bulk_capture as bulk
from app.config import Settings
from app.services.erp_pricing import preview_pricing
from scripts import run_erp_capture as cli
from test_erp_bulk_capture import xlsx


def scoped_capture(*, include_blocked=False):
    capture=json.loads((Path(__file__).parent/'fixtures/erp-pricing-capture.sample.json').read_text())
    if not include_blocked:
        capture['groups']=[g for g in capture['groups'] if g['source'].startswith('loss_')]
    capture['salesperson']={'name':'测试销售员','id':'USER-1'}
    for g in capture['groups']:
        if g['source'].startswith('loss_'):
            g['filters']['username']='测试销售员'
        else:
            g['filters']['shopUsernameid']='USER-1'
        for row in g['rows']:row['fields']['销售员']='测试销售员'
    return capture


def test_verified_salesperson_selector_is_a_valid_scope_not_full_four_sources():
    report=preview_pricing(scoped_capture())
    assert report['consistent_capture_scope']
    assert next(r for r in report['source_coverage'] if r['source']=='loss_allocatable')['finance_complete']
    assert '采集来源的筛选范围不一致或无法确认' not in report['global_issues']
    assert any('候选名单未取全' in issue and '禁止发货' in issue for issue in report['global_issues'])
    assert not report['execution_allowed']


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


@pytest.mark.parametrize('issue', [None, 'wrong_id', 'missing_filter', 'wrong_finance_owner'])
def test_four_source_preview_checks_blocked_seller_id_and_finance_name(issue):
    capture = scoped_capture(include_blocked=True)
    group = next(g for g in capture['groups'] if g['source'] == 'blocked_pending')
    if issue == 'wrong_id':
        group['filters']['shopUsernameid'] = 'USER-2'
    elif issue == 'missing_filter':
        del group['filters']['shopUsernameid']
    elif issue == 'wrong_finance_owner':
        group['rows'][0]['fields']['销售员'] = '其他人'

    report = preview_pricing(capture)
    assert report['consistent_capture_scope'] is (issue in (None, 'wrong_finance_owner'))
    coverage = {row['source']: row['finance_complete'] for row in report['source_coverage']}
    assert coverage['blocked_pending'] is (issue is None)
    if issue is None:
        assert len(coverage) == 4 and all(coverage.values())


@pytest.mark.parametrize(('source', 'issue'), [
    (None, None), ('blocked_pending', None), ('blocked_pending', 'missing_id'),
    ('blocked_allocatable', 'other_list_owner'), ('blocked_pending', 'missing_list_owner'),
    ('blocked_pending', 'other_finance_owner'), ('blocked_allocatable', 'missing_finance_owner'),
])
def test_salesperson_cli_checks_order_ownership_before_finance_and_on_download(
    tmp_path, monkeypatch, source, issue
):
    seller = '莫雯舒'
    submissions = []
    headers = ['订单编号', '订单状态', '平台', '集团', '销售员', '毛利', '修正毛利', '修正一次毛利']

    def sheet(order, owner):
        status = '可配货' if order.endswith('4') else '待处理'
        return xlsx(headers, [[order, status, 'Tiktok', '集团二部', owner, -1, -1, -1]])

    def post(path, body, binary=False, *, page_evidence=None):
        if path == '/Api/daysell/selectSellman':
            return [{'userName': seller, 'userID': None if issue == 'missing_id' else '007'}]
        if path == bulk.LOSS_EXPORT:
            assert body['username'] == seller and body['financecheck'] == '3'
            return sheet(f"L{body['status']}", seller)
        if path == bulk.BLOCKED_LIST:
            assert body['shopUsernameid'] == '007' and body['jzfh'] == '1'
            assert 'username' not in body and 'financecheck' not in body
            row = {'orderid': f"B{body['status']}", 'status': body['status'],
                   'platform': 'Tiktok', 'jzfh': '1'}
            if issue != 'missing_list_owner':
                row['shopUsername'] = '其他人' if issue == 'other_list_owner' else seller
            records = [row]
            if issue in ('other_list_owner', 'missing_list_owner'):
                records.append(dict(row, orderid=f"GOOD{body['status']}", shopUsername=seller))
            return {'total': len(records), 'records': records}
        if path == bulk.ORDER_EXPORT:
            order, = body['orderIdList']
            submissions.append(order)
            return '下载任务【核对】后台执行中'
        assert path == bulk.TASKS
        return [{'id': '1', 'fileName': '核对', 'source': 'orderList',
                 'status': 'success', 'downloadUrl': '/finance.xlsx'}]

    def download(_):
        owners = {'other_finance_owner': '其他人', 'missing_finance_owner': None}
        return sheet(submissions[-1], owners.get(issue, seller))

    settings = Settings(_env_file=None, erp_username='测试账号', erp_password='test')
    monkeypatch.setattr(cli, 'get_settings', lambda: settings)
    monkeypatch.setattr(bulk, '_session', lambda *args: nullcontext((post, download)))
    args = ['--salesperson', seller, '--all', '--output', str(tmp_path)]
    if source:
        args.extend(['--source', source])
    list_owner_issue = issue in ('other_list_owner', 'missing_list_owner')
    complete = issue is None or list_owner_issue
    assert cli.main(args) == (0 if complete else 1)
    run_dir, = tmp_path.glob('erp-capture-*')
    meta = json.loads((run_dir / 'run.json').read_text())
    archive = json.loads(next(run_dir.glob('source-captures/*/result.json')).read_text())
    expected_sources = [source] if source else [
        'loss_allocatable', 'loss_pending', 'blocked_allocatable', 'blocked_pending',
    ]
    assert meta['sources'] == [g['source'] for g in archive['groups']] == expected_sources
    assert archive['capture_complete'] is complete
    if list_owner_issue:
        group, = archive['groups']
        status = group['status']
        assert group['list_complete'] and group['error'] is None
        assert submissions == [f'GOOD{status}']
        assert [row['orderid'] for row in group['rows']] == [f'GOOD{status}']
        assert group['rows'][0]['fields']['销售员'] == seller
        excluded, = group['excluded_rows']
        assert excluded['order_id'] == f'B{status}'
        assert excluded['validation_reason'] == 'salesperson_mismatch'
        assert group['pages'][0]['excluded_rows'] == 1
        assert not group['failed_pages']
    elif issue is None:
        assert submissions == (['B3'] if source else ['L4', 'L3', 'B4', 'B3'])
        assert all(g['rows'][0]['fields']['销售员'] == seller for g in archive['groups'])
    else:
        group, = archive['groups']
        assert group['error'] and not group['rows']
        if issue == 'missing_id':
            assert not group['list_complete'] and not submissions
        else:
            assert group['list_complete'] and group['batches'][0]['status'] == 'failed'
