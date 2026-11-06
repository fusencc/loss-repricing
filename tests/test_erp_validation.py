"""只读诊断使用合成输入；通过不构成真实权限、业务口径或公式验收。"""
from copy import deepcopy

import pytest

from app.adapters.erp_validation import build_sample_validation


def sample():
    return {
        'source_version': 'synthetic-v1', 'queried_at': '2026-11-05T09:00:00+00:00',
        'pending_confirmations': ['亏损口径待确认'],
        'groups': [
            {'kind': kind, 'status': status, 'endpoint': '/synthetic/list',
             'filters': {'status': status, 'page': 1, 'limit': 2}, 'error': None,
             'rows': [{'orderid': 'O-1', 'shopid': 'S-1'}], 'total': 50}
            for kind in ('loss', 'blocked') for status in (4, 3)
        ],
        'details': [{'orderid': 'O-1', 'error': None,
                     'rows': [{'sku': 'SKU-1', 'qty': '2', 'currency': 'PHP'}]}],
    }


def test_report_never_promotes_sample_to_complete_or_authorized():
    data = sample()
    original = deepcopy(data)
    report = build_sample_validation(data)
    assert data == original
    assert report['authentication'] == 'authenticated'
    assert report['sample_only'] is True
    assert report['complete'] is report['pricing_ready'] is report['permission_scope_verified'] is False
    assert [g['source'] for g in report['groups']] == [
        'loss_allocatable', 'loss_pending', 'blocked_allocatable', 'blocked_pending']
    assert all(g['coverage'] == 'first_page_only' for g in report['groups'])
    assert all(g['observed_shop_ids'] == ['S-1'] and not g['permission_scope_verified'] for g in report['groups'])
    assert all(g['observed_order_ids'] == ['O-1'] for g in report['groups'])
    assert report['summary'] == {
        'list_reads_succeeded': 4, 'list_reads_failed': 0, 'sampled_rows': 4,
        'detail_reads_succeeded': 1, 'detail_reads_failed': 0,
        'detail_rows': 1, 'detail_issue_count': 0,
    }
    assert '亏损口径待确认' in report['pending_confirmations']
    assert any('公司正常一次毛利' in x for x in report['pending_confirmations'])


@pytest.mark.parametrize('total,rows,coverage', [
    (0, [], 'reported_empty'), (1, [{'orderid': 'O-1'}], 'reported_total_observed'),
    (None, [], 'total_unknown'), (True, [], 'total_unknown'),
    (-1, [], 'total_unknown'), ('10', [], 'total_unknown'),
    (0, [{'orderid': 'O-1'}], 'count_inconsistent'),
    (5, [], 'count_inconsistent'),
])
def test_reported_total_never_proves_global_visibility(total, rows, coverage):
    data = sample()
    data['groups'][0].update(total=total, rows=rows)
    group = build_sample_validation(data)['groups'][0]
    assert group['coverage'] == coverage
    assert group['permission_scope_verified'] is False


@pytest.mark.parametrize('value', [None, '', ' ', 0, -1, True, 'NaN', 'Infinity', 'not-a-number'])
def test_invalid_quantity_is_diagnostic_not_zero_or_batch_failure(value):
    data = sample()
    data['details'][0]['rows'][0]['qty'] = value
    report = build_sample_validation(data)
    assert report['summary']['list_reads_succeeded'] == 4
    assert report['detail_issues'][0]['fields'] == ['qty']
    assert report['summary']['detail_reads_succeeded'] == 1


def test_failed_and_missing_identity_and_empty_details_are_distinct():
    data = sample()
    data['groups'][0].update(error='请求被拒绝', error_kind='access_denied', http_status=403, rows=[], total=None)
    data['groups'][1]['rows'] = [{'orderid': None}, {'orderid': ' ', 'shopid': ' '}]
    data['details'] += [
        {'orderid': 'O-2', 'error': '读取超时', 'rows': []},
        {'orderid': None, 'error': '缺少订单标识', 'rows': []},
        {'orderid': 'O-3', 'error': None, 'rows': []},
        {'orderid': 'O-4', 'error': None, 'rows': [{'sku': None, 'currency': 'php', 'qty': 1}]},
    ]
    report = build_sample_validation(data)
    assert report['groups'][0]['coverage'] == 'read_failed'
    assert report['groups'][0]['http_status'] == 403
    assert report['groups'][1]['missing_order_ids'] == 2
    assert report['groups'][1]['observed_shop_ids'] == []
    assert report['summary']['list_reads_failed'] == 1
    assert report['summary']['detail_reads_failed'] == 1
    assert report['summary']['detail_reads_succeeded'] == 3
    assert report['summary']['detail_issue_count'] == 4
    assert report['detail_issues'][-1]['fields'] == ['sku', 'currency']


def test_validation_route_is_admin_only_and_does_not_write_execution(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select
    from app.config import Settings
    from app.main import create_app
    from app.models import Batch, Item, Execution
    from types import SimpleNamespace
    import app.api.erp_rates as routes
    import app.api.auth as auth

    config = Settings(_env_file=None, runtime_dir=tmp_path,
                      database_url=f"sqlite:///{tmp_path / 'validation.db'}",
                      admin_password='test-password', session_secret='s' * 40)
    calls = []
    report = build_sample_validation(sample())

    def capture(settings, *, limit):
        calls.append(limit)
        return {'validation': report, 'details': [{'private_raw_field': 'not-for-report'}]}

    monkeypatch.setattr(routes, 'capture_erp_sample', capture)
    app = create_app(config)
    with TestClient(app) as client:
        assert client.get('/api/sources/erp/validation').status_code == 401
        assert calls == []
        client.post('/api/auth/login', json={'username': 'admin', 'password': 'test-password'})
        assert client.get('/api/sources/erp/validation?limit=11').status_code == 422
        assert calls == []
        response = client.get('/api/sources/erp/validation')
        assert response.status_code == 200 and response.json() == report
        assert response.headers['cache-control'] == 'no-store'
        assert 'not-for-report' not in response.text
        assert calls == [2]
        with app.state.factory() as session:
            for model in (Batch, Item, Execution):
                assert session.scalar(select(func.count()).select_from(model)) == 0
        assert app.state.config.live_submit_enabled is False
        monkeypatch.setattr(auth, 'require_user', lambda r: SimpleNamespace(role='sales'))
        assert client.get('/api/sources/erp/validation').status_code == 403
        assert calls == [2]


def test_cli_report_only_excludes_raw_details(tmp_path, monkeypatch, capsys):
    import json
    import sys
    from types import SimpleNamespace
    import app.capture_erp as cli

    data = sample()
    data['details'][0]['rows'][0]['costPrice'] = 'raw-cost-not-for-report'
    data['validation'] = build_sample_validation(data)
    monkeypatch.setattr(cli, 'get_settings', lambda: SimpleNamespace(runtime_dir=tmp_path))
    limits = []

    def capture(settings, *, limit):
        limits.append(limit)
        return data

    monkeypatch.setattr(cli, 'capture_erp_sample', capture)
    monkeypatch.setattr(sys, 'argv', ['capture_erp', '--limit', '2', '--report-only'])
    cli.main()
    paths = list((tmp_path / 'source-captures').glob('*.json'))
    assert len(paths) == 1 and paths[0].name.startswith('erp-validation-')
    saved = paths[0].read_text(encoding='utf-8')
    assert json.loads(saved) == data['validation']
    assert 'raw-cost-not-for-report' not in saved
    assert limits == [2]
    assert 'permission_scope_verified=False' in capsys.readouterr().out


def test_validation_route_preserves_sanitized_source_failure(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.adapters.base import SourceUnavailable
    from app.config import Settings
    from app.main import create_app
    import app.api.erp_rates as routes

    def failed(*args, **kwargs):
        raise SourceUnavailable('请配置ERP账号')

    monkeypatch.setattr(routes, 'capture_erp_sample', failed)
    config = Settings(_env_file=None, runtime_dir=tmp_path,
                      database_url=f"sqlite:///{tmp_path / 'failure.db'}",
                      admin_password='test-password', session_secret='s' * 40)
    with TestClient(create_app(config)) as client:
        client.post('/api/auth/login', json={'username': 'admin', 'password': 'test-password'})
        response = client.get('/api/sources/erp/validation')
        assert response.status_code == 503
        assert response.json()['detail'] == '请配置ERP账号'
