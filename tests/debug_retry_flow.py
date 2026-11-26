"""Explicit-run diagnostics: mocked HTTP, simulated time, and a temporary worker database."""
import json
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx

from app import worker as worker_module
from app.adapters import erp_web, sales_batch_export
from app.adapters.base import SourceUnavailable
from app.adapters.http import HttpAdapter
from app.config import Settings
from app.db import Base, create_database
from app.models import Job
from app.services.store import enqueue


# region agent log
def _debug(hypothesis, location, message, data):
    with Path('/home/developer/projects/loss-repricing/.cursor/debug-b2f7e3.log').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'sessionId': 'b2f7e3', 'runId': 'post-fix',
                                 'hypothesisId': hypothesis, 'location': location,
                                 'message': message, 'data': data,
                                 'timestamp': time.time_ns() // 1_000_000}, ensure_ascii=False) + '\n')
# endregion


def test_worker_slow_reconcile_with_ready_job(tmp_path, monkeypatch):
    engine, factory = create_database(f"sqlite:///{tmp_path / 'debug-worker.db'}")
    Base.metadata.create_all(engine)
    elapsed = 0
    timeline = []
    clock = SimpleNamespace(monotonic=lambda: elapsed, time=lambda: 1_800_000_000 + elapsed)
    monkeypatch.setattr(worker_module, 'time', clock)

    def reconcile():
        nonlocal elapsed
        timeline.append(['reconcile_start', elapsed])
        elapsed += 70
        timeline.append(['reconcile_end', elapsed])

    def run_batch(_):
        timeline.append(['run_start', elapsed])

    service = SimpleNamespace(reconcile=reconcile, run_batch=run_batch,
                              config=Settings(_env_file=None, adapter_mode='file', runtime_dir=tmp_path))
    try:
        worker = worker_module.Worker(factory, service)
        first = enqueue(factory, 'run', {})
        worker.tick()
        elapsed += 5
        second = enqueue(factory, 'run', {})
        worker.tick()
        with factory() as session:
            states = [session.get(Job, key).state for key in (first, second)]
        # region agent log
        _debug('H1', 'worker.py:Worker._tick', '慢核验与可领取任务的实际顺序',
               {'timeline_seconds': timeline, 'job_states': states})
        # endregion
    finally:
        engine.dispose()


def test_sales_export_late_query_failure(monkeypatch):
    elapsed = 0
    query_times, checkpoints, submissions = [], [], 0

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    def route(request):
        nonlocal submissions
        if request.url.path == sales_batch_export.EXPORT:
            submissions += 1
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'id': 'simulated'}})
        assert request.url.path == sales_batch_export.TASKS
        query_times.append(elapsed)
        if elapsed >= 85:
            return httpx.Response(503)
        return httpx.Response(200, json={'resp_code': 0, 'datas': {'data': []}})

    def checkpoint(state):
        checkpoints.append((state['state'], len(state['read_failures'])))

    monkeypatch.setattr(sales_batch_export, 'monotonic', lambda: elapsed)
    monkeypatch.setattr(sales_batch_export, 'sleep', advance)
    auth = SimpleNamespace(collection_headers=dict, observe_collection_response=lambda _: None)
    outcome = 'returned'
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        try:
            sales_batch_export._export_file(client, auth, ['1000001'], 'synthetic', checkpoint=checkpoint)
        except (httpx.HTTPError, ValueError, SourceUnavailable) as err:
            outcome = type(err).__name__
    late_queries = [value for value in query_times if value >= 85]
    # region agent log
    _debug('H2', 'sales_batch_export.py:_export_file', '临近轮询结束时任务查询返回 503',
           {'elapsed_seconds': elapsed, 'first_503_second': min(late_queries, default=None),
            'last_query_second': max(query_times, default=None),
            'requests_after_90_seconds': sum(value > 90 for value in query_times),
            'creates': submissions, 'last_checkpoint': checkpoints[-1] if checkpoints else None,
            'outcome': outcome})
    # endregion


def test_erp_read_backoff_and_connect_timeout():
    def observe(failure, delays=None):
        elapsed = 0
        calls = []

        def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        request = httpx.Request('GET', 'https://example.invalid/')

        def read():
            calls.append(elapsed)
            if len(calls) < 3:
                if failure == 'connect_timeout':
                    raise httpx.ConnectTimeout('simulated', request=request)
                httpx.Response(503, request=request).raise_for_status()

        kwargs = {'delays': delays} if delays is not None else {}
        outcome = 'returned'
        try:
            erp_web.retry_erp_read(read, wait=advance, **kwargs)
        except httpx.HTTPError as err:
            outcome = type(err).__name__
        return {'request_seconds': calls, 'outcome': outcome}

    # region agent log
    _debug('H3', 'erp_web.py:retry_erp_read', 'ERP 只读失败的尝试间隔',
           {'default_503': observe('503'), 'default_connect_timeout': observe('connect_timeout'),
            'blocked_list_503': observe('503', erp_web.BLOCKED_LIST_RETRY_DELAYS)})
    # endregion


def test_http_list_when_total_changes():
    def observe(second_total):
        pages = []
        spec = {'as_of_path': 'as_of', 'business_date_path': 'business_date',
                'records_path': 'rows', 'pagination': {'max_pages': 5, 'total_path': 'total', 'limit': 2}}

        def fetch(_spec, _params, *, page):
            pages.append(page)
            return {'as_of': '2026-11-26T09:00:00+08:00', 'business_date': '2026-11-26',
                    'total': 3 if page == 1 else second_total,
                    'rows': {1: [1, 2], 2: [3], 3: []}[page]}

        outcome, returned = 'returned', None
        try:
            _, rows = HttpAdapter._list_source(SimpleNamespace(_fetch=fetch), spec, {}, date(2026, 11, 26))
            returned = len(rows)
        except SourceUnavailable as err:
            outcome = type(err).__name__
        return {'requested_pages': pages, 'rows_returned': returned, 'outcome': outcome}

    # region agent log
    _debug('H4', 'http.py:HttpAdapter._list_source', '同样的两页数据，仅修改第二页的总数',
           {'stable_total': observe(3), 'changed_total': observe(4)})
    # endregion
