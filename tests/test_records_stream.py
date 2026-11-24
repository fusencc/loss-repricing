"""隔离数据库验证逐条提交可见性；不请求真实改价接口。"""
import asyncio
import json

from app.models import Batch, Item, Execution
from app.services.records_stream import records_payload, record_events
from test_records_api import client  # 同一组隔离日志/归属样本。


class Connected:
    async def is_disconnected(self):
        return False


def test_stream_emits_committed_upload_and_verification_before_batch_finishes(client, monkeypatch):
    monkeypatch.setattr('app.services.records_stream.POLL_SECONDS', 0)
    factory = client.app.state.factory
    department = 'department:Tiktok一部'

    async def check():
        initial = records_payload(factory, department)
        stream = record_events(Connected(), lambda: records_payload(factory, department), initial)
        try:
            assert await anext(stream) == f'data: {initial}\n\n'
            assert await anext(stream) == ': keepalive\n\n'
            with factory.begin() as session:
                session.get(Batch, 'b0').status = 'running'
                item = session.get(Item, 'i0')
                item.status, item.phase = '待核验', 'accepted'
                item.updated_at = '2026-11-24T13:48:00+00:00'
                session.add(Execution(id='live-upload', item_id=item.id, variant_id=item.variant_id,
                    state='accepted', request={}, response={'status': 'accepted'}, evidence={},
                    created_at=item.updated_at, updated_at=item.updated_at))
            accepted = json.loads((await anext(stream)).removeprefix('data: '))
            row = next(row for row in accepted['items'] if row['id'] == 'i0')
            assert row['status'] == '待核验'
            with factory.begin() as session:
                item = session.get(Item, 'i0')
                item.status, item.phase = '已核验', 'verified'
                item.updated_at = '2026-11-24T13:48:01+00:00'
                execution = session.get(Execution, 'live-upload')
                execution.state, execution.updated_at = 'verified', item.updated_at
                execution.evidence = {'actual_price': '15.12', 'confirmed_at': item.updated_at}
            verified = json.loads((await anext(stream)).removeprefix('data: '))
            assert next(row for row in verified['items'] if row['id'] == 'i0')['status'] == '已核验'
            with factory() as session:
                assert session.get(Batch, 'b0').status == 'running'
            # 重连不依赖丢失事件，首帧直接是最新已保存状态。
            reconnected = record_events(Connected(), lambda: records_payload(factory, department), records_payload(factory, department))
            try:
                assert json.loads((await anext(reconnected)).removeprefix('data: ')) == verified
            finally:
                await reconnected.aclose()
        finally:
            await stream.aclose()
    asyncio.run(check())


def test_stream_ignores_uncommitted_data_and_keeps_department_scope(client):
    factory = client.app.state.factory
    department = 'department:Tiktok一部'
    initial = records_payload(factory, department)
    with factory() as session:
        item = session.get(Item, 'i0')
        item.reason = '尚未提交，禁止展示'
        session.flush()
        assert records_payload(factory, department) == initial
        session.rollback()
    payload = json.loads(initial)
    assert {row['id'] for row in payload['items']} == {'i0', 'i3'}
    assert 'private-password' not in initial
    assert 'private-seller' not in initial


def test_stream_route_headers_and_invalid_department(client, monkeypatch):
    async def finite(request, read_payload, initial):
        yield f'data: {initial}\n\n'
    monkeypatch.setattr('app.services.records_stream.record_events', finite)
    response = client.get('/api/records/stream', params={'department': 'department:Tiktok一部'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    assert response.headers['x-accel-buffering'] == 'no'
    assert response.headers['cache-control'] == 'no-store'
    assert json.loads(response.text.removeprefix('data: ')) == client.get('/api/records').json()
    assert client.get('/api/records/stream', params={'department': '不存在'}).status_code == 404


def test_stream_stops_querying_when_disconnected():
    class Disconnected:
        async def is_disconnected(self):
            return True
    def unexpected(*args):
        raise AssertionError('断连后不得继续查库')

    async def check():
        stream = record_events(Disconnected(), unexpected, '{}')
        assert await anext(stream) == 'data: {}\n\n'
        assert [event async for event in stream] == []
    asyncio.run(check())


def test_real_http_stream_delivers_changes_without_waiting_for_connection_close(client):
    import socket
    import threading
    import httpx
    import uvicorn

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(client.app, lifespan='off', log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        with httpx.stream('GET', f'http://127.0.0.1:{port}/api/records/stream',
                          params={'department': 'department:Tiktok一部'}, timeout=10, trust_env=False) as response:
            assert response.status_code == 200
            lines = response.iter_lines()

            def snapshot():
                return json.loads(next(line[6:] for line in lines if line.startswith('data: ')))

            initial = snapshot()
            assert next(row for row in initial['items'] if row['id'] == 'i0')['status'] == '待处理'
            with client.app.state.factory.begin() as session:
                item = session.get(Item, 'i0')
                item.status, item.phase = '待核验', 'accepted'
                item.updated_at = '2026-11-24T13:50:00+00:00'
            updated = snapshot()
            assert next(row for row in updated['items'] if row['id'] == 'i0')['status'] == '待核验'
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()


def test_standalone_live_stream_matches_poll_projection_and_display_bindings(client, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from runpy import run_path
    from pathlib import Path

    with client.app.state.factory.begin() as session:
        session.get(Batch, 'b0').mode = 'live'
        key = session.get(Item, 'i0').key
    binding = {'key': key, 'seller': {'id': None, 'name': '已确认展示归属'}}
    bindings = tmp_path / 'display.json'
    bindings.write_text(json.dumps([binding]), encoding='utf-8')
    module = run_path(str(Path(__file__).resolve().parents[1] / 'deploy/serve-records-live.py'))
    app = module['create_records_app'](tmp_path / 'records.db', bindings)
    department = {'department': 'department:Tiktok一部'}

    async def first_frame(request, read_payload, initial):
        yield f'data: {initial}\n\n'

    monkeypatch.setattr('app.services.records_stream.record_events', first_frame)
    with TestClient(app) as standalone:
        polled = standalone.get('/api/records', params=department)
        assert polled.json()['items'][0]['seller'] == binding['seller']
        response = standalone.get('/api/records/stream', params=department)
        assert response.status_code == 200
        assert response.headers['x-accel-buffering'] == 'no'
        assert json.loads(response.text.removeprefix('data: ')) == polled.json()
        assert standalone.get('/api/records/stream', params={'department': '不存在'}).status_code == 404
