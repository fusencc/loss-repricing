"""日志故障及权限边界不应改变现有执行业务语义。"""
import json
from uuid import uuid4
from types import SimpleNamespace

from fastapi.testclient import TestClient
from app.api.auth import make_session
from app.adapters.base import LookupResult, SourceUnavailable
from app.adapters.demo import DemoAdapter
from app.config import Settings
from app.main import create_app
from app.models import Execution, VariantGuard
from app.observability import bind_sink, configure_logging, event
from app.services.execution import PricingService
from app.services.store import update_settings


def setup(tmp_path):
    cfg = Settings(database_url=f'sqlite:///{tmp_path}/safety.db', runtime_dir=tmp_path,
                   admin_password='test-password', session_secret='k' * 40,
                   coordination_required=False, _env_file=None)
    return cfg, create_app(cfg)


def login(client, cfg):
    client.cookies.set('lr_session', make_session(cfg.admin_username, cfg.session_secret))


def test_non_admin_cannot_query_export_or_see_status(tmp_path, monkeypatch):
    from app.api import auth
    cfg, app = setup(tmp_path)
    monkeypatch.setattr(auth, 'require_user', lambda request: SimpleNamespace(role='sales'))
    with TestClient(app) as client:
        for url in ('/api/logs', '/api/logs/status', '/api/logs/errors/' + uuid4().hex):
            assert client.get(url).status_code == 403
        assert client.post('/api/logs/export', json={}).status_code == 403


def test_logging_failure_never_releases_unknown_lock_or_reuploads(tmp_path, monkeypatch):
    cfg, app = setup(tmp_path)
    class UncertainAdapter(DemoAdapter):
        calls = 0
        def submit(self, execution_id, decision):
            self.calls += 1
            raise TimeoutError('private-upstream-token-marker')
        def lookup(self, *args):
            return LookupResult('unknown')
    adapter = UncertainAdapter(tmp_path)
    service = PricingService(app.state.factory, adapter, cfg)
    update_settings(app.state.factory, {'autonomous_enabled': True,
                    'pilot_shops': ['DEMO-MY'], 'site_enabled': ['MY']})
    def cannot_log(*args):
        raise OSError('disk failure')
    monkeypatch.setattr(app.state.log_sink, 'write', cannot_log)
    service.run_batch('first')
    calls = adapter.calls
    assert calls > 0
    service.run_batch('second')
    service.reconcile()
    assert adapter.calls == calls
    with app.state.factory() as session:
        assert all(row.state == 'unknown' for row in session.query(Execution).all())
        assert session.query(VariantGuard).filter(VariantGuard.execution_id.is_not(None)).count() > 0
    app.state.engine.dispose()


def test_handled_http_error_number_is_queryable(tmp_path):
    cfg, app = setup(tmp_path)
    with TestClient(app) as client:
        rejected = client.get('/api/overview')
        assert rejected.status_code == 401
        error_id = rejected.headers['X-Error-ID']
        login(client, cfg)
        located = client.get('/api/logs/errors/' + error_id)
        assert located.status_code == 200
        assert located.json()['error']['http_status'] == 401


def test_validation_error_has_id_without_logging_input_value(tmp_path):
    cfg, app = setup(tmp_path)
    with TestClient(app) as client:
        login(client, cfg)
        response = client.get('/api/sources/erp/sample', params={'limit': 'sensitive-invalid-input-marker'})
        assert response.status_code == 422
        assert len(response.headers['X-Error-ID']) == 32
    assert 'sensitive-invalid-input-marker' not in app.state.log_sink.path.read_text()


def test_real_read_only_erp_logged_as_real_even_in_demo(tmp_path, monkeypatch):
    from app.api import erp_rates
    cfg, app = setup(tmp_path)
    monkeypatch.setattr(erp_rates, 'query_erp_rates', lambda config: {'rates': []})
    with TestClient(app) as client:
        login(client, cfg)
        result = client.get('/api/sources/erp/rates')
        assert result.status_code == 200
    rows = [json.loads(line) for line in app.state.log_sink.path.read_text().splitlines()]
    erp_rows = [row for row in rows if row['event'].startswith('erp.')]
    assert erp_rows
    assert all(row['mode'] == 'demo' and row['source'] == 'erp' and row['synthetic'] is False for row in erp_rows)
    assert len({row['operation_id'] for row in erp_rows}) == 1
    assert all(row.get('request_id') == result.headers['X-Request-ID'] for row in erp_rows)


def test_unexpected_erp_failure_reuses_original_error_id_in_body_and_header(tmp_path, monkeypatch):
    from app.api import erp_rates
    cfg, app = setup(tmp_path)
    def unexpected(config):
        raise RuntimeError('private-unexpected-erp-marker')
    monkeypatch.setattr(erp_rates, 'query_erp_rates', unexpected)
    with TestClient(app) as client:
        login(client, cfg)
        response = client.get('/api/sources/erp/rates')
        assert response.status_code == 500
        assert response.json()['error_id'] == response.headers['X-Error-ID']
        failure = client.get('/api/logs/errors/' + response.headers['X-Error-ID']).json()['error']
        assert failure['stack'][-1]['function'] == 'unexpected'
    assert 'private-unexpected-erp-marker' not in app.state.log_sink.path.read_text()


def test_log_capacity_warning_preserves_active_file_and_business_file(tmp_path):
    cfg, app = setup(tmp_path)
    sink = app.state.log_sink
    business = tmp_path / 'receipt.json'
    business.write_text('business-evidence')
    sink.total_bytes = 1
    sink.cleanup()
    assert sink.path.exists() and business.read_text() == 'business-evidence'
    assert sink.cleanup_ok is False
    assert sink.status()['usage_truncated'] is False
    app.state.engine.dispose()
