import sqlite3
from pathlib import Path
from runpy import run_path

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.models import Batch, Item
from app.services.records import department_records
from test_records_api import client


def test_only_selected_department_materializes_full_item_details(client):
    loaded = []
    def track(item, context):
        loaded.append(item.id)
    event.listen(Item, 'load', track)
    try:
        with client.app.state.factory() as session:
            data = department_records(session, 'department:Tiktok一部')
    finally:
        event.remove(Item, 'load', track)
    assert [row['id'] for row in data['items']] == ['i3', 'i0']
    assert set(loaded) == {'i0', 'i3'}
    assert next(d for d in data['departments'] if d['name'] == 'Tiktok五部')['total'] == 1


def test_capture_timestamp_reads_index_without_opening_finance_archive(client, tmp_path):
    capture_id = 'erp-capture-20261127T010000000000Z-' + 'a' * 32
    timestamp = '2026-11-27T01:00:00+00:00'
    with sqlite3.connect(tmp_path / 'erp-clean.sqlite3') as db:
        db.execute('CREATE TABLE captures (capture_id TEXT PRIMARY KEY, queried_at TEXT)')
        db.execute('INSERT INTO captures VALUES (?, ?)', (capture_id, timestamp))
    with client.app.state.factory.begin() as session:
        batch = session.get(Batch, 'b0')
        batch.mode = 'live'
        batch.run_key = f'erp-roster:history:测试员:{batch.business_date}'
        batch.snapshot = {
            'source_versions': {'erp_capture_id': capture_id,
                'erp_roster_salesperson': '测试员', 'erp_roster_department': 'Tiktok一部'},
            'erp_roster_filter_log': {'capture_id': capture_id,
                'salesperson': '测试员', 'department': 'Tiktok一部'},
        }
        session.get(Item, 'i0').status = '不调价'
    with client.app.state.factory() as session:
        data = department_records(session, 'department:Tiktok一部', runtime_dir=tmp_path)
    assert next(row for row in data['items'] if row['id'] == 'i0')['display_time'] == timestamp
    assert not (tmp_path / 'source-captures').exists()


def test_unchanged_database_does_not_rebuild_view_after_thirty_seconds(client, tmp_path, monkeypatch):
    module = run_path(str(Path(__file__).resolve().parents[1] / 'deploy/serve-records-live.py'))
    calls = []
    original = module['department_records']
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    module['create_records_app'].__globals__['department_records'] = counted
    clock = [100.0]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    app = module['create_records_app'](tmp_path / 'records.db', tmp_path / 'bindings.json')
    with TestClient(app) as standalone:
        first = standalone.get('/api/records').json()
        clock[0] += 31
        assert standalone.get('/api/records').json() == first
        assert len(calls) == 1
        with client.app.state.factory.begin() as session:
            session.get(Batch, 'b0').mode = 'live'
        clock[0] += 6
        assert standalone.get('/api/records').json()['total'] == 1
        assert len(calls) == 2


def test_cached_view_stays_responsive_during_another_refresh(client, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    module = run_path(str(Path(__file__).resolve().parents[1] / 'deploy/serve-records-live.py'))
    clock = [100.0]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    original = module['department_records']
    entered, release = Event(), Event()
    def slow(session, department, **kwargs):
        if department == 'department:Tiktok五部':
            entered.set()
            assert release.wait(10)
        return original(session, department, **kwargs)
    module['create_records_app'].__globals__['department_records'] = slow
    app = module['create_records_app'](tmp_path / 'records.db', tmp_path / 'bindings.json')
    with TestClient(app) as standalone, ThreadPoolExecutor(max_workers=2) as pool:
        first = standalone.get('/api/records').json()
        clock[0] += 6
        pending = pool.submit(standalone.get, '/api/records?department=department:Tiktok五部')
        try:
            assert entered.wait(5)
            cached = pool.submit(standalone.get, '/api/records')
            assert cached.result(timeout=2).json() == first
        finally:
            release.set()
            pending.result(timeout=5)


def test_continuous_commits_are_coalesced_for_five_seconds(client, tmp_path, monkeypatch):
    module = run_path(str(Path(__file__).resolve().parents[1] / 'deploy/serve-records-live.py'))
    clock = [100.0]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    app = module['create_records_app'](tmp_path / 'records.db', tmp_path / 'bindings.json')
    with TestClient(app) as standalone:
        first = standalone.get('/api/records').json()
        with client.app.state.factory.begin() as session:
            session.get(Batch, 'b0').mode = 'live'
        clock[0] += 1
        assert standalone.get('/api/records').json() == first
        clock[0] += 4
        assert standalone.get('/api/records').json()['total'] == 1
