import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from app.services import erp_capture_store as store


def example():
    return {'source_version': 'test', 'queried_at': '2026-11-06T01:00:00+00:00',
            'pricing_ready': False, 'capture_complete': False,
            'groups': [{'kind': 'loss', 'status': 4, 'rows': [{'orderid': '001234567890123456789'}]}],
            'details': [{'orderid': '001234567890123456789', 'error': None,
                         'rows': [{'sku': '中文SKU', 'inventory': None, 'costPrice': '1.234567890123456789'}]}],
            'summary': {'list_rows': 1, 'detail_rows': 1}}


def test_new_archive_rejects_retired_per_order_result(tmp_path):
    with pytest.raises(ValueError, match='批量采集'):
        store.save_capture(tmp_path, example())
    assert list(tmp_path.iterdir()) == []


def saved_historical_archive(runtime_dir):
    capture_id = 'erp-capture-20261106T010000000000Z-' + uuid4().hex
    path = runtime_dir / 'source-captures' / (capture_id + '.json')
    store.write_verified(path, dict(example(), capture_id=capture_id))
    return capture_id


def test_existing_archive_roundtrip_preserves_precision_and_nulls(tmp_path):
    capture_id = saved_historical_archive(tmp_path)
    restored = store.load_capture(tmp_path, capture_id)
    assert restored['groups'] == example()['groups']
    assert restored['details'] == example()['details']
    order = store.find_saved_order(tmp_path, capture_id, '001234567890123456789')
    assert order['groups'][0]['kind'] == 'loss'
    assert order['detail']['rows'][0]['inventory'] is None
    assert order['detail']['rows'][0]['costPrice'] == '1.234567890123456789'
    with pytest.raises(KeyError):
        store.find_order(restored, 'absent')


def test_failed_json_write_leaves_last_checkpoint_in_place(tmp_path):
    path = tmp_path / 'result.json'
    store.write_verified(path, {'status': 'complete'})
    with pytest.raises(TypeError):
        store.write_verified(path, {'invalid': object()})
    assert json.loads(path.read_text(encoding='utf-8')) == {'status': 'complete'}
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('capture_id', ['../outside', '/tmp/file', 'invalid'])
def test_lookup_rejects_paths_outside_capture_directory(tmp_path, capture_id):
    with pytest.raises(ValueError):
        store.load_capture(tmp_path, capture_id)


def test_cli_order_read_uses_saved_file_without_erp_login(tmp_path, monkeypatch, capsys):
    from app import capture_erp as cli
    from app.config import Settings

    capture_id = saved_historical_archive(tmp_path)
    monkeypatch.setattr(cli, 'get_settings', lambda: Settings(_env_file=None, runtime_dir=tmp_path))
    monkeypatch.setattr(cli, 'capture_erp_bulk', lambda *_a, **_kw: pytest.fail('读取归档不应请求 ERP'))
    monkeypatch.setattr(sys, 'argv', ['capture_erp', '--capture-id', capture_id, '--order', '001234567890123456789'])
    cli.main()
    assert json.loads(capsys.readouterr().out)['detail']['rows'][0]['sku'] == '中文SKU'


def test_api_capture_persists_and_lookup_requires_admin(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    import app.api.erp_rates as routes
    calls = []
    def capture(settings, **kwargs):
        calls.append(kwargs)
        result = dict(example(), acquisition_mode='bulk_download', details=[])
        group = result['groups'][0]
        group.update(source='blocked_pending', kind='blocked', status=3)
        group['rows'][0]['fields'] = {'修正一次毛利': '-1.25'}
        return result
    monkeypatch.setattr(routes, 'capture_erp_bulk', capture)
    settings = Settings(_env_file=None, runtime_dir=tmp_path, database_url=f'sqlite:///{tmp_path / "api.db"}',
                        admin_password='test-password', session_secret='s'*40)
    with TestClient(create_app(settings)) as client:
        assert client.post('/api/sources/erp/captures').status_code == 401
        client.post('/api/auth/login', json={'username': 'admin', 'password': 'test-password'})
        response = client.post('/api/sources/erp/captures', params={'source': 'blocked_pending'})
        assert response.status_code == 200, response.text
        data = response.json()
        assert 'storage_verified' not in data
        assert data['storage_saved'] and len(calls) == 1 and calls[0]['source'] == 'blocked_pending'
        url = '/api/sources/erp/captures/' + data['capture_id']
        assert client.get(url).json()['details'] == []
        saved_order = client.get(url, params={'orderid': '001234567890123456789'}).json()
        assert saved_order['groups'][0]['rows'][0]['fields']['修正一次毛利'] == '-1.25'
        assert client.get(url, params={'orderid': 'absent'}).status_code == 404
        client.post('/api/auth/logout')
        assert client.get(url).status_code == 401


def test_order_lookup_reuses_decoded_capture_and_detects_replacement(tmp_path, monkeypatch):
    capture_id = saved_historical_archive(tmp_path)
    order_id = example()["details"][0]["orderid"]
    path = tmp_path / "source-captures" / (capture_id + ".json")
    reads = []
    read_text = Path.read_text

    def observe(path, *args, **kwargs):
        reads.append(path)
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", observe)
        first = store.find_saved_order(tmp_path, capture_id, order_id)
        first["detail"]["rows"][0]["sku"] = "caller changed result"
        second = store.find_saved_order(tmp_path, capture_id, order_id)
        assert second["detail"]["rows"][0]["sku"] == "中文SKU"
        assert reads == [path]

    replacement = dict(example(), capture_id=capture_id)
    replacement["details"][0]["rows"][0]["sku"] = "new version"
    store.write_verified(path, replacement)
    assert store.find_saved_order(tmp_path, capture_id, order_id)["detail"]["rows"][0]["sku"] == "new version"
    path.unlink()
    with pytest.raises(FileNotFoundError):
        store.find_saved_order(tmp_path, capture_id, order_id)


def test_cached_lookup_revalidates_compressed_archive(tmp_path, monkeypatch):
    import gzip

    capture_id = saved_historical_archive(tmp_path)
    root = tmp_path / "source-captures"
    folder = root / "erp-bulk-20261110T000000Z-0123456789abcdef0123456789abcdef"
    folder.mkdir()
    archive = folder / "result.json.gz"
    archive.write_bytes(gzip.compress(json.dumps(dict(example(), capture_id=capture_id)).encode()))
    store.write_verified(root / (capture_id + ".json"), {"capture_id": capture_id, "bulk_archive": folder.name})
    reads = []
    reader = store._read_archive

    def observe(path):
        reads.append(path)
        return reader(path)

    monkeypatch.setattr(store, "_read_archive", observe)
    order_id = example()["details"][0]["orderid"]
    expected = store.find_order(dict(example(), capture_id=capture_id), order_id)
    assert store.find_saved_order(tmp_path, capture_id, order_id) == expected
    assert store.find_saved_order(tmp_path, capture_id, order_id) == expected
    assert reads == [archive]
    archive.write_bytes(b"broken")
    with pytest.raises(OSError):
        store.find_saved_order(tmp_path, capture_id, order_id)
