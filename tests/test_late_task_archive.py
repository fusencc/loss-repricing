"""使用隔离数据库和模拟外部回执回放晚到失败。"""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, time
from types import SimpleNamespace

from openpyxl import load_workbook

from app.adapters.base import LookupResult, SubmitResult
from app.domain.engine import evaluate
from app.models import Execution, Item
from app.services import morning_log, notifications
from app.services.store import BEIJING, enqueue, update_settings
from app.worker import Worker
from test_department_execution import department  # noqa: F401
from test_department_upload_regression import offline_http  # noqa: F401


def test_late_task_archive_replay(department, offline_http, monkeypatch):
    service, factory, prepared, transport, _, submitted = department
    config, day = service.config, prepared.day
    config.ding_tk_morning_log_folder = 'fake-detail'
    config.ding_cancellation_log_folder = 'fake-success'
    config.ding_drive_space_id = ''
    config.ding_enabled = True
    config.ding_client_id = config.ding_client_secret = 'fixture-only'
    update_settings(factory, {'schedule_enabled': False})
    now = datetime.combine(day, time(9), BEIJING)

    second = next(d for d in evaluate(prepared.snapshot, prepared.snapshot.as_of)
                  if d.ready and d.key != prepared.decisions[0].key)
    prepared.prices[second.key.identity()] = replace(next(iter(prepared.prices.values())),
        upload_price=second.upload_price, target_actual=second.target_actual)
    prepared.decisions.append(second)
    order_ids = set()
    for decision in prepared.decisions:
        decision.trace['source_versions']['erp_capture_id'] = 'fixture-capture'
        order_ids.update(row['order_id'] for row in decision.trace['source_rows'])
    monkeypatch.setattr('app.services.erp_capture_store.load_capture', lambda *_: {'groups': [{'rows': [
        {'orderid': order, 'fields': {'订单编号': order, '店铺': '测试店铺', '销售员': '测试销售员'}}
        for order in sorted(order_ids)
    ]}]})

    price_queries, message_sends = [], []
    outcome = 'unknown'

    def submit(ident, _decision):
        submitted.append(ident)
        return SubmitResult('accepted', 'fixture-task')

    def lookup(ident, _task, _decision):
        result = outcome if ident == submitted[0] else 'unknown'
        return LookupResult(result, message='模拟任务明确失败' if result == 'rejected' else '')

    def price_lookup(*_):
        price_queries.append(1)
        raise AssertionError('任务结果轮询不应查询现价')

    monkeypatch.setattr(transport, 'submit', submit)
    monkeypatch.setattr(transport, 'lookup', lookup)
    monkeypatch.setattr(service.adapter, 'lookup', price_lookup, raising=False)
    monkeypatch.setattr('app.worker.run_daily_departments', lambda service, job, _: service.run_department_batch(job.id, prepared))
    monkeypatch.setattr(notifications, 'utcnow', lambda: now)
    monkeypatch.setattr(notifications, 'watch_operations', lambda *_: None)
    monkeypatch.setattr(notifications, 'resolve_salesperson_userid', lambda *_: {'userid': 'fixture-user'})

    def send_notice(*_):
        message_sends.append(1)
        return {'state': 'accepted', 'task_id': 'fixture-message'}

    monkeypatch.setattr(notifications, 'DingTalkClient', lambda _: SimpleNamespace(
        send_notice=send_notice, query_notice=lambda *_: {'state': 'accepted'}, close=lambda: None))

    published, filenames, uploads = {}, {}, []
    folders = {'morning-logs': 'fake-detail', 'success-logs': 'fake-success'}

    def drive(args, directory):
        folder = folders[directory.parent.name]
        node = f'{folder}-node'
        if args[1] == '+list':
            return {'files': [{'name': filenames[folder], 'nodeId': node, 'type': 'FILE'}]
                    if folder in filenames else [], 'hasMore': False}
        assert args[1] == '+upload'
        target = args[args.index('--node') + 1] if '--node' in args else None
        uploads.append((folder, target))
        filenames[folder] = args[args.index('--file') + 1]
        with closing(load_workbook(directory / filenames[folder], read_only=True)) as book:
            if folder == 'fake-detail':
                published[folder] = [row[8] for row in list(book['明细'].values)[1:]]
            else:
                published[folder] = sum(1 for _ in book['成功清单'].values) - 1
        return {'nodeId': node}

    monkeypatch.setattr(morning_log, '_dws', drive)
    worker = Worker(factory, service)
    enqueue(factory, 'run', {'date': day.isoformat()}, f'daily:{day.isoformat()}')
    worker.tick(now.timestamp())
    notifications.dispatch_notices(factory, config)
    assert len(submitted) == 2 and len(message_sends) == 1
    assert published['fake-detail'] == ['已提价', '已提价'] and published['fake-success'] > 0

    service.reconcile(tasks_only=True)
    worker.tick(now.timestamp() + 61)
    assert len(uploads) == 2
    outcome = 'rejected'
    service.reconcile(tasks_only=True)
    with factory() as session:
        execution = session.get(Execution, submitted[0])
        item = session.get(Item, execution.item_id)
        assert execution.state == 'failed' and item.status == '待处理'

    worker.tick(now.timestamp() + 122)
    notifications.dispatch_notices(factory, config)
    assert not price_queries and len(submitted) == 2
