from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.adapters.base import LookupResult, SourceUnavailable
from app.config import Settings
from app.db import Base, create_database, transaction
from app.domain.engine import evaluate
from app.models import Batch, Execution, Item, Job
from app.services import erp_roster_pricing as roster
from app.services.erp_capture_store import write_verified
from app.services.execution import PricingService
from app.services.store import BEIJING, enqueue, update_settings
from fake_adapter import DemoAdapter


@pytest.fixture
def priced_roster(tmp_path, monkeypatch):
    class SalesTestTransport(DemoAdapter):
        mode = 'live'
        synthetic = False
        submits = 0
        readback_ready = False

        def collect(self, day):
            pytest.fail('名单批次不能走通用采集')

        def readiness(self):
            return {'contract_ready': False, 'verified_sites': []}

        def submit(self, execution_id, decision):
            pytest.fail('名单批次不能走通用上传')

        def lookup(self, *args):
            pytest.fail('名单批次不能走通用回读')

        def accept_single(self, execution_id, decision):
            self.submits += 1
            super().submit(execution_id, decision)
            raise TimeoutError('模拟平台已接收，但回复中断')

        def read_single(self, *args):
            if not self.readback_ready:
                return LookupResult('unknown')
            return super().lookup(*args)

    def history_unavailable(*args):
        raise ValueError('测试中不访问调价历史')

    monkeypatch.setattr('app.services.execution.capture_recent_price_changes', lambda *args, **kw: {'source_id': 'test'})
    monkeypatch.setattr('app.services.execution.recent_price_log_for', history_unavailable)
    engine, factory = create_database(f'sqlite:///{tmp_path / "live.db"}')
    Base.metadata.create_all(engine)
    day = datetime.now(timezone.utc).astimezone(BEIJING).date()
    config = Settings(_env_file=None, runtime_dir=tmp_path, live_submit_enabled=True, coordination_required=False,
                      erp_roster_date=day, erp_roster_submit_date=day)
    adapter = SalesTestTransport(tmp_path)
    service = PricingService(factory, adapter, config)

    class SingleChannel:
        available = True

        def prepare(self, execution_id, batch_id, business_date, shop_name, decision):
            assert business_date == day and shop_name == 'ERP测试店'
            return {'scope': decision.trace['source_versions']['erp_roster_salesperson'],
                    'business_date': business_date.isoformat(),
                    'observed_at': datetime.now(timezone.utc).isoformat()}

        def submit(self, execution_id, decision):
            return adapter.accept_single(execution_id, decision)

        def lookup(self, execution_id, task_id, decision):
            return adapter.read_single(execution_id, task_id, decision.key)

    channel = SingleChannel()
    monkeypatch.setattr(PricingService, '_roster_transport', lambda self: channel)
    initial = DemoAdapter(tmp_path).collect(day)
    initial.lines, initial.online = initial.lines[:2], initial.online[:1]
    person = next(iter(roster.ERP_ROSTER_DEPARTMENTS))
    department = roster.ERP_ROSTER_DEPARTMENTS[person]

    def archive(snapshot, *, name=person, sample=False):
        capture_id = 'erp-capture-20261125T010000000000Z-' + uuid4().hex
        capture_file = tmp_path / 'source-captures' / f'{capture_id}.json'
        capture_file.parent.mkdir(exist_ok=True)
        write_verified(capture_file, {'capture_id': capture_id, 'source_kind': 'erp_web_backend',
                       'sample_only': sample, 'salesperson': {'name': name}, 'department': department})
        snapshot = snapshot.model_copy(deep=True)
        snapshot.source_versions['erp_capture_id'] = capture_id
        preview_id = uuid4().hex
        preview = tmp_path / 'erp-pricing-previews' / f'{preview_id}.json'
        preview.parent.mkdir(exist_ok=True)
        write_verified(preview, {'capture_id': capture_id, 'preview_id': preview_id,
            'department': department, 'business_date': day.isoformat(), 'synthetic': False,
            'consistent_capture_scope': True, '_snapshot': snapshot.model_dump(mode='json'),
            'references': {'lines': {row.row_id: {'target': row.key.model_dump(mode='json'),
                            'main_sku': row.main_sku, 'erp_shop': 'ERP测试店'} for row in snapshot.lines}},
            'decisions': [row.model_dump(mode='json') for row in evaluate(snapshot, snapshot.as_of)]})
        _, digest = roster._load_pricing_artifact(tmp_path, capture_id, preview_id)
        return {'capture_id': capture_id, 'preview_id': preview_id, 'preview_sha256': digest,
                'mode': 'capture', 'salesperson': person, 'department': department, 'ready_prices': 1}

    yield factory, service, adapter, day, person, initial, archive
    engine.dispose()


def test_roster_passes_verified_archive_to_existing_submit_and_recovers_readback(priced_roster, monkeypatch):
    factory, service, adapter, day, person, initial, archive = priced_roster
    key = initial.online[0].key
    update_settings(factory, {'autonomous_enabled': True, 'site_enabled': [key.site], 'pilot_shops': [key.shop_id]})
    cleaned = archive(initial)

    def recapture(config, *, roster_date, roster_person):
        assert not config.live_submit_enabled
        assert (roster_date, roster_person) == (day, person)
        return {'departments': [archive(initial)]}

    monkeypatch.setattr(roster, 'run_workflow', recapture)
    result = roster.run_roster_pricing(service, 'day-job', day, cleaned)
    assert result['state'] == 'evaluated'
    with factory() as session:
        batch = session.get(Batch, result['batch_id'])
        execution = session.scalars(select(Execution)).one()
        assert batch.snapshot['source_versions']['erp_roster_preview'] == cleaned['preview_id']
        assert execution.state == 'unknown'
    adapter.readback_ready = True
    PricingService(factory, adapter, service.config).reconcile()
    roster.run_roster_pricing(service, 'day-job', day, cleaned)
    with factory() as session:
        assert session.scalars(select(Execution)).one().state == 'verified'
        assert session.scalars(select(Item)).one().status == '已核验'
    assert adapter.submits == 1


@pytest.mark.parametrize('invalid', ['sample', 'person', 'hash'])
def test_roster_does_not_submit_unbound_archive(priced_roster, invalid):
    factory, service, adapter, day, _, initial, archive = priced_roster
    kwargs = {'sample': True} if invalid == 'sample' else {'name': '其他人员'} if invalid == 'person' else {}
    scope = archive(initial, **kwargs)
    if invalid == 'hash':
        scope['preview_sha256'] = '0' * 64
    result = roster.run_roster_pricing(service, 'day-job', day, scope)
    assert result['state'] == 'blocked'
    with factory() as session:
        assert session.scalar(select(Batch)) is None
    assert adapter.submits == 0


def test_fixed_roster_scope_crossing_day_does_not_enqueue_general_task(priced_roster):
    from datetime import timedelta

    factory, service, adapter, day, _, _, _ = priced_roster
    with pytest.raises(SourceUnavailable, match='跨北京时间'):
        service.run_batch('roster', roster=roster.RosterBatch(
            day - timedelta(days=1), '桂露', lambda _: pytest.fail('跨日不得重新取数'), {},
        ))
    with factory() as session:
        assert session.scalar(select(Job)) is None
    assert adapter.submits == 0



@pytest.mark.parametrize('pause', [None, 'global', 'site'])
def test_roster_without_date_permission_or_paused_preserves_batch_and_has_no_upload(priced_roster, monkeypatch, pause):
    factory, service, adapter, day, _, initial, archive = priced_roster
    if pause == 'global':
        update_settings(factory, {'paused': True})
    elif pause == 'site':
        update_settings(factory, {'paused_sites': [initial.online[0].key.site]})
    else:
        service.config = service.config.model_copy(update={'erp_roster_submit_date': None})
    monkeypatch.setattr(roster, 'run_workflow', lambda *args, **kwargs: pytest.fail('已暂停或未授权不能刷新后提交'))
    result = roster.run_roster_pricing(service, 'day-job', day, archive(initial))
    with factory() as session:
        item = session.scalars(select(Item).where(Item.batch_id == result['batch_id'])).one()
        assert item.status == '待处理'
        assert session.scalar(select(Execution)) is None
    assert adapter.submits == 0


def test_roster_changed_amount_during_refresh_is_held(priced_roster, monkeypatch):
    factory, service, adapter, day, _, initial, archive = priced_roster
    key = initial.online[0].key
    update_settings(factory, {'autonomous_enabled': True, 'site_enabled': [key.site], 'pilot_shops': [key.shop_id]})
    changed = initial.model_copy(deep=True)
    for line in changed.lines:
        line.loss_amount -= 1
    monkeypatch.setattr(roster, 'run_workflow', lambda *args, **kwargs: {'departments': [archive(changed)]})
    roster.run_roster_pricing(service, 'day-job', day, archive(initial))
    with factory() as session:
        item = session.scalars(select(Item)).one()
        assert item.status == '待处理'
        assert '重算结果已变化' in item.reason
        assert session.scalar(select(Execution)) is None
    assert adapter.submits == 0



def test_roster_retry_cannot_expand_first_saved_price_authorization(priced_roster, monkeypatch):
    factory, service, adapter, day, _, initial, archive = priced_roster
    service.config = service.config.model_copy(update={'erp_roster_submit_date': None})
    original = roster.run_roster_pricing(service, 'day-job', day, archive(initial))
    service.config = service.config.model_copy(update={'erp_roster_submit_date': day})
    changed = initial.model_copy(deep=True)
    for line in changed.lines:
        line.loss_amount -= 1
    monkeypatch.setattr(roster, 'run_workflow', lambda *args, **kwargs: pytest.fail('重试不能扩大原拟价范围'))
    retry = roster.run_roster_pricing(service, 'day-job', day, archive(changed))
    assert retry['batch_id'] == original['batch_id']
    with factory() as session:
        assert session.scalar(select(Execution)) is None
    assert adapter.submits == 0


def test_enabled_roster_uses_single_upload_and_recovers_without_generic_contract(priced_roster, monkeypatch):
    factory, service, adapter, day, person, initial, archive = priced_roster
    service.config = service.config.model_copy(update={
        'erp_roster_date': day, 'erp_roster_submit_date': day, 'coordination_required': True,
    })
    monkeypatch.setattr(roster, 'run_workflow',
                        lambda *args, **kwargs: {'departments': [archive(initial)]})

    result = roster.run_roster_pricing(service, 'day-authorized', day, archive(initial))
    with factory() as session:
        execution = session.scalar(select(Execution))
        item = session.scalars(select(Item)).one()
        assert execution is not None, item.reason
        assert execution.state == 'unknown'
        assert execution.request['trace']['erp_roster_upload']['scope'] == person
    adapter.readback_ready = True
    # 日期授权关闭后，仍使用原单品入口回读已递交的请求。
    after = service.config.model_copy(update={'erp_roster_submit_date': None})
    PricingService(factory, adapter, after).reconcile()
    with factory() as session:
        assert session.scalars(select(Execution)).one().state == 'verified'
    assert result['batch_id'] and adapter.submits == 1


def test_roster_does_not_override_manual_takeover(priced_roster, monkeypatch):
    from app.services.price_control import lock_control

    factory, service, adapter, day, _, initial, archive = priced_roster
    service.config = service.config.model_copy(update={'coordination_required': True})
    with factory.begin() as session:
        lock_control(session, initial.online[0].key).mode = 'manual'
    monkeypatch.setattr(roster, 'run_workflow',
                        lambda *args, **kwargs: pytest.fail('已人工接管的商品不能准备提交'))
    roster.run_roster_pricing(service, 'authorized-day', day, archive(initial))
    with factory() as session:
        assert session.scalars(select(Item)).one().reason == '人工接管或交回后等待重新取数'
        assert session.scalar(select(Execution)) is None
    assert adapter.submits == 0


def test_roster_does_not_submit_site_warehouse_without_verified_transport(priced_roster, monkeypatch):
    factory, service, adapter, day, _, _, archive = priced_roster
    snapshot = DemoAdapter(service.config.runtime_dir).collect(day)
    # PH 直发可按业务公式算价，已验证的 PH 单品上传及回读证据覆盖海外仓。
    online = snapshot.online[1]
    snapshot.lines = [row for row in snapshot.lines if row.key == online.key]
    snapshot.online = [online]
    monkeypatch.setattr(roster, 'run_workflow',
                        lambda *args, **kwargs: pytest.fail('不适用的站点仓配不应该准备提交'))
    roster.run_roster_pricing(service, 'authorized-day', day, archive(snapshot))
    with factory() as session:
        assert '站点与仓配' in session.scalars(select(Item)).one().reason
        assert session.scalar(select(Execution)) is None
    assert adapter.submits == 0


def test_stock_resume_requeues_same_daily_job_once(priced_roster):
    factory, service, _, day, _, _, _ = priced_roster
    manifest = 'a' * 32
    folder = service.config.runtime_dir / 'erp-workflows' / manifest
    folder.mkdir(parents=True)
    write_verified(folder / 'result.json', {'roster_date': str(day), 'status': 'needs_attention',
        'departments': [{'salesperson': name} for name in roster.ERP_ROSTER_DEPARTMENTS]})
    job_id = enqueue(factory, 'run', {'date': str(day)}, f'daily:{day}')
    with pytest.raises(SourceUnavailable, match='仍在运行或等待重试'):
        roster.requeue_roster_stock_resume(factory, service.config.runtime_dir, day, manifest)
    with transaction(factory) as session:
        session.get(Job, job_id).state = 'done'
    for _ in range(2):
        assert roster.requeue_roster_stock_resume(factory, service.config.runtime_dir, day, manifest) == job_id
    with factory() as session:
        job = session.scalars(select(Job)).one()
        assert (job.state, job.payload['resume_roster_workflow']) == ('queued', manifest)


def test_stock_resume_reuses_archived_erp_inputs_with_fresh_single_sku_preflight(priced_roster, monkeypatch):
    factory, service, adapter, day, _, initial, archive = priced_roster
    online = initial.online[0]
    online.observed_at = initial.as_of - timedelta(minutes=10)
    monkeypatch.setattr(roster, 'run_workflow', lambda *a, **k: pytest.fail('续跑不重新采集 ERP'))
    result = roster.run_roster_pricing(service, 'resume-job', day, archive(initial),
                                      zero_stock_targets={online.key.target_identity()})
    assert result['state'] == 'evaluated'
    with factory() as session:
        assert session.scalars(select(Execution)).one().state == 'unknown'
    assert adapter.submits == 1
