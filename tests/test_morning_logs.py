from datetime import date
from contextlib import closing
import pytest
import json

from openpyxl import Workbook, load_workbook

from app.config import Settings
from app.db import Base, create_database
from app.models import Batch, Item, Execution
from app.services import morning_log


DAY = date(2026, 11, 25)
HEADERS = ('处理时间', '销售员', 'SKU', '商品ID', '产品ID', '店铺ID', '站点', '仓配',
           '状态', '原实际营销价', '目标实际营销价', '上传价', '币种', '处理说明')


def setup_log(tmp_path, monkeypatch):
    engine, factory = create_database(f'sqlite:///{tmp_path / "logs.db"}')
    Base.metadata.create_all(engine)
    config = Settings(_env_file=None, runtime_dir=tmp_path)
    export = tmp_path / 'loss_allocatable-order-finance-001.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['订单编号', '交易号', '店铺', '销售员'])
    sheet.append(['erp-order', '0587373171290572533', 'Tiktok-583TH', '杨颖'])
    sheet.append(['unknown-order', '0587373171290572534', 'Tiktok-583TH', '杨颖'])
    book.save(export)
    monkeypatch.setattr('app.services.erp_capture_store.load_capture', lambda *_: {
        'groups': [{'files': [{'file': str(export)}]}]})
    with factory.begin() as session:
        session.add(Batch(id='batch', run_key='erp-roster:job:杨颖:2026-11-25',
                          business_date='2026-11-25', mode='live', status='completed',
                          created_at='2026-11-25T01:00:00+00:00',
                          snapshot={'source_versions': {'erp_roster_salesperson': '杨颖',
                                                       'erp_roster_department': 'Tiktok十二部'}}, summary={}))
        session.flush()
        for n, state in enumerate(('verified', 'unknown', None)):
            key = dict(platform='TikTok', shop_id='1919695864650514433', site='TH',
                       product_id='1738438776194172241', item_id=f'173843884476462011{n}',
                       sku=f'SKU-{n}', warehouse='东莞仓库')
            decision = {'key': key, 'current_actual': '1.23499', 'target_actual': '2.345',
                        'upload_price': '4.6800', 'trace': {'station_currency': 'THB',
                        'source_versions': {'erp_capture_id': 'capture'}, 'source_rows': [
                            {'order_id': 'erp-order' if n == 0 else 'unknown-order'},
                            {'order_id': 'erp-order' if n == 0 else 'unknown-order'}]}}
            session.add(Item(id=f'item-{n}', batch_id='batch', variant_id=f'variant-{n}', key=key,
                             status='已核验' if state == 'verified' else '待核验' if state else '不调价',
                             phase='verified' if state == 'verified' else 'pending', reason='持久原因',
                             decision=decision, updated_at='2026-11-25T02:00:00+00:00'))
            session.flush()
            if state:
                session.add(Execution(id=f'exec-{n}', item_id=f'item-{n}', variant_id=f'variant-{n}',
                                      state=state, request=decision, response={},
                                      evidence={'actual_price': '2.34', 'confirmed_at': '2026-11-25T02:00:00+00:00'},
                                      created_at='2026-11-25T01:00:00+00:00', updated_at='2026-11-25T02:00:00+00:00'))
    return engine, factory, config


def test_detail_log_has_fourteen_columns_and_regenerates_after_upload_acceptance(tmp_path, monkeypatch):
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    path = morning_log.save_morning_log(config, DAY, factory=factory)
    with closing(load_workbook(path)) as book:
        rows = list(book['明细'].values)
        assert book.sheetnames == ['明细']
        assert rows[0] == HEADERS
        assert len(rows) == 4
        by_sku = {row[2]: row for row in rows[1:]}
        assert by_sku['SKU-0'][1:8] == ('杨颖', 'SKU-0', '1738438844764620110', '1738438776194172241',
                                  '1919695864650514433', 'TH', '东莞仓库')
        assert by_sku['SKU-0'][9:12] == ('1.23', '2.35', '4.68')
        assert by_sku['SKU-1'][8] == '待核验'
        assert by_sku['SKU-1'][-1] == '上传结果待确认，请勿重复上传'
    with factory.begin() as session:
        execution = session.get(Execution, 'exec-1')
        execution.state, execution.response = 'accepted', {'status': 'accepted'}
        execution.evidence = {}
    morning_log.save_morning_log(config, DAY, factory=factory)
    with closing(load_workbook(path)) as book:
        assert {row[2]: row[8] for row in list(book['明细'].values)[1:]}['SKU-1'] == '已提价'
    engine.dispose()


@pytest.mark.parametrize(('state', 'receipt', 'row_status', 'priced'), [
    ('accepted', 'accepted', None, True), ('uploaded', 'accepted', None, True),
    ('mismatch', 'accepted', None, True), ('mismatch', 'unknown', None, False),
    ('submitting', 'accepted', None, False), ('failed', 'accepted', 'rejected', False),
])
def test_success_log_refresh_uses_accepted_upload_not_price_readback(tmp_path, monkeypatch, state, receipt, row_status, priced):
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    path = morning_log.save_success_log(config, DAY, factory=factory)
    initial = path.read_bytes()
    morning_log.save_success_log(config, DAY, factory=factory)
    assert path.read_bytes() == initial
    with factory.begin() as session:
        execution = session.get(Execution, 'exec-1')
        execution.state = state
        execution.response, execution.evidence = {'status': receipt}, {'row_status': row_status}
    morning_log.save_success_log(config, DAY, factory=factory)
    with closing(load_workbook(path)) as book:
        assert [row[0] for row in list(book['成功清单'].values)[1:]] == (
            ['0587373171290572533', '0587373171290572534'] if priced else ['0587373171290572533'])
    engine.dispose()


def test_missing_trade_number_skips_only_that_order(tmp_path, monkeypatch, caplog):
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    with factory.begin() as session:
        execution = session.get(Execution, 'exec-0')
        request = json.loads(json.dumps(execution.request))
        request['trace']['source_rows'].append({'order_id': 'missing-order'})
        execution.request = request
    path = morning_log.save_success_log(config, DAY, factory=factory)
    with closing(load_workbook(path)) as book:
        assert [row[0] for row in list(book['成功清单'].values)[1:]] == ['0587373171290572533']
    assert '交易号缺失或不唯一' in caplog.text
    engine.dispose()


def test_background_readback_refreshes_both_logs_without_running_price_submission(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from app.worker import Worker
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    config = config.model_copy(update={'ding_tk_morning_log_folder': 'own',
        'ding_cancellation_log_folder': 'luodi', 'erp_roster_schedule_enabled': False})
    calls = []
    monkeypatch.setattr('app.services.notifications.dispatch_notices', lambda *_: None)
    monkeypatch.setattr(morning_log, 'publish_morning_log', lambda *_, success=False: calls.append(success))
    def reconcile():
        with factory.begin() as session:
            session.get(Item, 'item-1').status = '已核验'
            session.get(Execution, 'exec-1').state = 'verified'
    worker = Worker(factory, SimpleNamespace(config=config, reconcile=reconcile))
    worker.tick(datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc).timestamp())
    assert calls == [False, True]
    with closing(load_workbook(tmp_path / 'success-logs' / DAY.isoformat() / '11.25早上运行日志.xlsx')) as book:
        assert len(list(book['成功清单'].values)) == 3
    engine.dispose()


def test_success_log_uses_real_trade_number_dedupes_and_excludes_unknown(tmp_path, monkeypatch):
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    path = morning_log.save_success_log(config, DAY, factory=factory)
    assert path.name == '11.25早上运行日志.xlsx'
    assert path.parent != morning_log.save_morning_log(config, DAY, factory=factory).parent
    with closing(load_workbook(path)) as book:
        assert book.sheetnames == ['成功清单', '失败清单', '汇总']
        assert list(book['成功清单'].values) == [('交易号', '店铺', '销售', '状态'),
            ('0587373171290572533', 'Tiktok-583TH', '杨颖', '提价成功')]
        assert list(book['失败清单'].values) == [('交易号', '店铺', '销售', '失败原因')]
        assert list(book['汇总'].values)[1][1:] == (1, 1, 0, 0)
    engine.dispose()


def test_two_destinations_are_independent_and_second_still_runs_after_failure(tmp_path, monkeypatch):
    from app.worker import Worker
    from types import SimpleNamespace
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    config = config.model_copy(update={'ding_tk_morning_log_folder': 'own',
                                       'ding_cancellation_log_folder': 'luodi'})
    calls = []
    def publish(config, filename, *, success=False):
        calls.append((success, filename.name))
        if not success:
            raise RuntimeError('own unavailable')
    monkeypatch.setattr(morning_log, 'publish_morning_log', publish)
    worker = Worker(factory, SimpleNamespace(config=config))
    assert worker._morning_log(config, DAY)
    assert calls == [(False, '11.25早上运行日志.xlsx'), (True, '11.25早上运行日志.xlsx')]
    engine.dispose()


def test_publisher_uses_each_confirmed_folder_and_numeric_parent(tmp_path, monkeypatch):
    config = Settings(_env_file=None, runtime_dir=tmp_path, ding_drive_space_id='123',
        ding_tk_morning_log_folder='own', ding_tk_drive_parent_id='456',
        ding_cancellation_log_folder='luodi', ding_cancellation_drive_parent_id='789')
    calls = []
    monkeypatch.setattr('app.adapters.dingtalk_drive.publish_drive_workbook',
                        lambda config, filename, folder, parent: calls.append((folder, parent)))
    morning_log.publish_morning_log(config, tmp_path / '11.25早上运行日志.xlsx')
    morning_log.publish_morning_log(config, tmp_path / '11.25早上运行日志.xlsx', success=True)
    assert calls == [('own', '456'), ('luodi', '789')]


def test_success_log_does_not_export_explicitly_excluded_order(tmp_path, monkeypatch):
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    with factory.begin() as session:
        execution = session.get(Execution, 'exec-0')
        request = json.loads(json.dumps(execution.request))
        request['trace']['source_rows'].append({'row_id': 'excluded', 'order_id': 'unknown-order'})
        request['trace']['excluded_source_rows'] = {'excluded': 'nonnegative_loss'}
        execution.request = request
    path = morning_log.save_success_log(config, DAY, factory=factory)
    with closing(load_workbook(path)) as book:
        assert len(list(book['成功清单'].values)) == 2
    engine.dispose()


def test_unknown_upload_keeps_original_workbook_when_records_change(tmp_path, monkeypatch):
    from hashlib import sha256
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    path = morning_log.save_morning_log(config, DAY, factory=factory)
    original = path.read_bytes()
    path.with_suffix('.publish-intent.json').write_text(json.dumps({
        'sha256': sha256(original).hexdigest(), 'folder_id': 'own'}))
    with factory.begin() as session:
        session.get(Item, 'item-1').status = '已核验'
    with pytest.raises(RuntimeError, match='待核验'):
        morning_log.save_morning_log(config, DAY, factory=factory)
    assert path.read_bytes() == original
    engine.dispose()


def test_cross_day_readback_refreshes_original_business_day(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from app.worker import Worker
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    with factory.begin() as session:
        batch = session.get(Batch, 'batch')
        batch.business_date = '2026-11-24'
        batch.run_key = 'erp-roster:job:杨颖:2026-11-24'
    config = config.model_copy(update={'ding_tk_morning_log_folder': 'own',
        'ding_cancellation_log_folder': 'luodi', 'erp_roster_schedule_enabled': False})
    calls = []
    monkeypatch.setattr(morning_log, 'publish_morning_log',
                        lambda config, path, success=False: calls.append((path.name, success)))
    def reconcile():
        with factory.begin() as session:
            session.get(Item, 'item-1').status = '已核验'
            session.get(Execution, 'exec-1').state = 'verified'
    Worker(factory, SimpleNamespace(config=config, reconcile=reconcile)).tick(
        datetime(2026, 11, 25, 8, tzinfo=timezone.utc).timestamp())
    assert calls == [('11.24早上运行日志.xlsx', False), ('11.24早上运行日志.xlsx', True)]
    engine.dispose()


def test_cli_attempts_second_destination_when_first_fails(tmp_path, monkeypatch):
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    monkeypatch.setattr('app.config.get_settings', lambda: config)
    monkeypatch.setattr('app.db.create_database', lambda *_: (engine, factory))
    calls = []
    def publish(config, path, *, success=False):
        calls.append(success)
        if not success:
            raise RuntimeError('own unavailable')
    monkeypatch.setattr(morning_log, 'publish_morning_log', publish)
    with pytest.raises(SystemExit) as result:
        morning_log.main(['2026-11-25'])
    assert result.value.code == 1
    assert calls == [False, True]


def test_cli_does_not_overwrite_replaced_node(tmp_path, monkeypatch):
    from hashlib import sha256
    path = tmp_path / '11.25早上运行日志.xlsx'
    path.write_bytes(b'changed')
    path.with_suffix('.receipt.json').write_text(json.dumps({
        'folder_id': 'own', 'node_id': 'original-node', 'sha256': sha256(b'old').hexdigest()}))
    calls = []
    monkeypatch.setattr(morning_log, '_dws', lambda args, _: calls.append(args) or {
        'files': [{'type': 'FILE', 'name': path.name, 'nodeId': 'replacement-node'}], 'hasMore': False})
    with pytest.raises(RuntimeError, match='归档文件'):
        morning_log.publish_workbook('own', path)
    assert len(calls) == 1 and calls[0][1] == '+list'


def test_historical_log_failure_is_retried_after_worker_restart(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from app.worker import Worker
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    with factory.begin() as session:
        batch = session.get(Batch, 'batch')
        batch.business_date = '2026-11-24'
        batch.run_key = 'erp-roster:job:杨颖:2026-11-24'
    config = config.model_copy(update={'ding_tk_morning_log_folder': 'own',
        'ding_cancellation_log_folder': 'luodi', 'erp_roster_schedule_enabled': False})
    calls = []
    unavailable = [True]
    def publish(config, path, success=False):
        calls.append((path.name, success))
        if unavailable[0]:
            raise RuntimeError('network unavailable')
    def reconcile():
        with factory.begin() as session:
            session.get(Item, 'item-1').status = '待处理'
            session.get(Execution, 'exec-1').state = 'failed'
    monkeypatch.setattr(morning_log, 'publish_morning_log', publish)
    worker = Worker(factory, SimpleNamespace(config=config, reconcile=reconcile))
    now = datetime(2026, 11, 25, 8, tzinfo=timezone.utc).timestamp()
    worker.tick(now)
    marker = tmp_path / 'morning-logs' / '2026-11-24' / 'refresh-pending.json'
    assert marker.exists()
    assert len(calls) == 2
    unavailable[0] = False
    # 同一个租约所有者模拟重启后的恢复路径，避免等待旧租约超时。
    restarted = Worker(factory, SimpleNamespace(config=config, reconcile=lambda: None))
    restarted.owner = worker.owner
    restarted.tick(now + 61)
    assert calls == [('11.24早上运行日志.xlsx', False), ('11.24早上运行日志.xlsx', True)] * 2
    assert not marker.exists()
    with closing(load_workbook(marker.parent / '11.24早上运行日志.xlsx')) as book:
        assert {row[2]: row[8] for row in list(book['明细'].values)[1:]}['SKU-1'] == '待处理'
    engine.dispose()


def test_marker_cleanup_failure_does_not_block_claiming_price_job(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from app.worker import Worker
    from app.services.store import enqueue
    engine, factory, config = setup_log(tmp_path, monkeypatch)
    config = config.model_copy(update={'ding_tk_morning_log_folder': 'own',
        'ding_cancellation_log_folder': 'luodi', 'erp_roster_schedule_enabled': False})
    original_unlink = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.name == 'refresh-pending.json':
            raise OSError('cannot remove marker')
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    monkeypatch.setattr(morning_log, 'publish_morning_log', lambda *_, **kw: None)
    calls = []
    enqueue(factory, 'run', {}, 'manual-log-isolation')
    def run(key):
        calls.append(key)
        return 'batch'
    service = SimpleNamespace(config=config, reconcile=lambda: None, run_batch=run)
    Worker(factory, service).tick(datetime(2026, 11, 25, 8, tzinfo=timezone.utc).timestamp())
    assert len(calls) == 1
    assert (tmp_path / 'morning-logs' / DAY.isoformat() / 'refresh-pending.json').exists()
    engine.dispose()
