from contextlib import contextmanager
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.adapters import dingtalk_drive


def drive(tmp_path, monkeypatch, *, fail=False, sparse=False):
    path = tmp_path / '11.25早上运行日志.xlsx'
    path.write_bytes(b'first')
    config = SimpleNamespace(ding_drive_space_id='123')
    calls = []
    current = {'uuid': 'file', 'id': '456', 'name': path.name, 'size': 5,
               'spaceId': '123', 'parentId': '789'}
    @contextmanager
    def folder(*_):
        def request(method, endpoint, payload=None, params=None):
            if method == 'GET':
                return {'dentries': [dict(current, type='FILE')]}
            if endpoint.endswith('/query'):
                return {'uploadKey': 'upload', 'headerSignatureInfo': {
                    'resourceUrls': ['https://upload.dingtalk.com/file'], 'headers': {}}}
            calls.append(payload['option']['conflictStrategy'])
            if fail:
                raise RuntimeError('unknown commit')
            current['size'] = len(path.read_bytes())
            return {'dentry': {'id': '456'} if sparse else current}
        def inspect(node_id):
            assert sparse and node_id == '456', '完整回执不应重复查询文件'
            calls.append('inspect')
            return current
        yield SimpleNamespace(http=SimpleNamespace(put=lambda *a, **kw: SimpleNamespace(is_success=True))), request, inspect
    monkeypatch.setattr(dingtalk_drive, 'drive_folder', folder)
    return config, path, calls


def test_changed_daily_log_updates_same_node_while_unchanged_log_is_not_uploaded(tmp_path, monkeypatch):
    config, path, calls = drive(tmp_path, monkeypatch)
    first = dingtalk_drive.publish_drive_workbook(config, path, 'folder', '789')
    dingtalk_drive.publish_drive_workbook(config, path, 'folder', '789')
    assert calls == ['OVERWRITE']
    path.write_bytes(b'second version')
    second = dingtalk_drive.publish_drive_workbook(config, path, 'folder', '789')
    assert calls == ['OVERWRITE', 'OVERWRITE']
    assert first['node_id'] == second['node_id'] == 'file'
    assert second['sha256'] == sha256(path.read_bytes()).hexdigest()
    assert not path.with_suffix('.publish-intent.json').exists()


def test_sparse_commit_receipt_queries_file_once(tmp_path, monkeypatch):
    config, path, calls = drive(tmp_path, monkeypatch, sparse=True)
    receipt = dingtalk_drive.publish_drive_workbook(config, path, 'folder', '789')
    assert receipt['node_id'] == 'file'
    assert calls == ['OVERWRITE', 'inspect']


def test_uncertain_commit_stops_reupload_and_does_not_claim_success(tmp_path, monkeypatch):
    config, path, calls = drive(tmp_path, monkeypatch, fail=True)
    with pytest.raises(RuntimeError, match='unknown commit'):
        dingtalk_drive.publish_drive_workbook(config, path, 'folder', '789')
    with pytest.raises(RuntimeError, match='禁止自动重传'):
        dingtalk_drive.publish_drive_workbook(config, path, 'folder', '789')
    assert len(calls) == 1
    assert not path.with_suffix('.receipt.json').exists()
