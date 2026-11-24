from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.adapters.base import LookupResult, SourceUnavailable
from app.config import Settings
from app.db import Base, create_database
from app.domain.engine import evaluate
from app.models import Batch, Execution, Item, Job
from app.services import erp_roster_pricing as roster
from app.services.erp_capture_store import write_verified
from app.services.execution import PricingService
from app.services.store import BEIJING, update_settings
from fake_adapter import DemoAdapter


@pytest.fixture
def priced_roster(tmp_path, monkeypatch):
    class SalesTestTransport(DemoAdapter):
        mode = 'live'
        submits = 0
        readback_ready = False

        def collect(self, day):
            pytest.fail('名单批次不能走通用采集')

        def readiness(self):
            return {'contract_ready': True, 'verified_sites': ['MY']}

        def submit(self, execution_id, decision):
            self.submits += 1
            super().submit(execution_id, decision)
            raise TimeoutError('模拟平台已接收，但回复中断')

        def lookup(self, *args):
            if not self.readback_ready:
                return LookupResult('unknown')
            return super().lookup(*args)

    def history_unavailable(*args):
        raise ValueError('测试中不访问调价历史')

    monkeypatch.setattr('app.services.execution.capture_recent_price_changes', lambda *args, **kw: {'source_id': 'test'})
    monkeypatch.setattr('app.services.execution.recent_price_log_for', history_unavailable)
    engine, factory = create_database(f'sqlite:///{tmp_path / "live.db"}')
    Base.metadata.create_all(engine)
    config = Settings(_env_file=None, runtime_dir=tmp_path, live_submit_enabled=True, coordination_required=False)
    adapter = SalesTestTransport(tmp_path)
    service = PricingService(factory, adapter, config)
    day = datetime.now(timezone.utc).astimezone(BEIJING).date()
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
            'decisions': [row.model_dump(mode='json') for row in evaluate(snapshot, snapshot.as_of)]})
        _, digest = roster._load_pricing_artifact(tmp_path, capture_id, preview_id)
        return {'capture_id': capture_id, 'preview_id': preview_id, 'preview_sha256': digest,
                'mode': 'capture', 'salesperson': person, 'department': department, 'ready_prices': 1}

    def capture(scope):
        return {'roster_date': day.isoformat(), 'departments': [
            scope if name == person else {'salesperson': name, 'department': dept, 'ready_prices': 0}
            for name, dept in roster.ERP_ROSTER_DEPARTMENTS.items()]}

    yield factory, service, adapter, day, person, initial, archive, capture
    engine.dispose()


def test_roster_passes_verified_archive_to_existing_submit_and_recovers_readback(priced_roster, monkeypatch):
    factory, service, adapter, day, person, initial, archive, capture = priced_roster
    key = initial.online[0].key
    update_settings(factory, {'autonomous_enabled': True, 'site_enabled': [key.site], 'pilot_shops': [key.shop_id]})
    cleaned = archive(initial)

    def recapture(config, *, roster_date, roster_person):
        assert not config.live_submit_enabled
        assert (roster_date, roster_person) == (day, person)
        return {'departments': [archive(initial)]}

    monkeypatch.setattr(roster, 'run_workflow', recapture)
    result = roster.run_roster_pricing(service, 'day-job', day, capture(cleaned))
    assert result[0]['state'] == 'evaluated'
    with factory() as session:
        batch = session.get(Batch, result[0]['batch_id'])
        execution = session.scalars(select(Execution)).one()
        assert batch.snapshot['source_versions']['erp_roster_preview'] == cleaned['preview_id']
        assert execution.state == 'unknown'
    adapter.readback_ready = True
    PricingService(factory, adapter, service.config).reconcile()
    roster.run_roster_pricing(service, 'day-job', day, capture(cleaned))
    with factory() as session:
        assert session.scalars(select(Execution)).one().state == 'verified'
        assert session.scalars(select(Item)).one().status == '已核验'
    assert adapter.submits == 1


@pytest.mark.parametrize('invalid', ['sample', 'person', 'hash'])
def test_roster_does_not_submit_unbound_archive(priced_roster, invalid):
    factory, service, adapter, day, _, initial, archive, capture = priced_roster
    kwargs = {'sample': True} if invalid == 'sample' else {'name': '其他人员'} if invalid == 'person' else {}
    scope = archive(initial, **kwargs)
    if invalid == 'hash':
        scope['preview_sha256'] = '0' * 64
    result = roster.run_roster_pricing(service, 'day-job', day, capture(scope))
    assert result[0]['state'] == 'blocked'
    with factory() as session:
        assert session.scalar(select(Batch)) is None
    assert adapter.submits == 0


def test_fixed_roster_scope_crossing_day_does_not_enqueue_general_task(priced_roster):
    from datetime import timedelta

    factory, service, adapter, day, _, _, _, _ = priced_roster
    with pytest.raises(SourceUnavailable, match='跨北京时间'):
        service.run_batch('roster', scoped_collect=lambda _: pytest.fail('跨日不得重新取数'),
                          fixed_day=day - timedelta(days=1), approved_prices={})
    with factory() as session:
        assert session.scalar(select(Job)) is None
    assert adapter.submits == 0



def test_roster_without_pilot_preserves_batch_and_has_no_upload(priced_roster, monkeypatch):
    factory, service, adapter, day, _, initial, archive, capture = priced_roster
    monkeypatch.setattr(roster, 'run_workflow', lambda *args, **kwargs: pytest.fail('缺少试点不能刷新后提交'))
    result = roster.run_roster_pricing(service, 'day-job', day, capture(archive(initial)))
    with factory() as session:
        item = session.scalars(select(Item).where(Item.batch_id == result[0]['batch_id'])).one()
        assert item.status == '待处理'
        assert session.scalar(select(Execution)) is None
    assert adapter.submits == 0


def test_roster_changed_amount_during_refresh_is_held(priced_roster, monkeypatch):
    factory, service, adapter, day, _, initial, archive, capture = priced_roster
    key = initial.online[0].key
    update_settings(factory, {'autonomous_enabled': True, 'site_enabled': [key.site], 'pilot_shops': [key.shop_id]})
    changed = initial.model_copy(deep=True)
    for line in changed.lines:
        line.loss_amount -= 1
    monkeypatch.setattr(roster, 'run_workflow', lambda *args, **kwargs: {'departments': [archive(changed)]})
    roster.run_roster_pricing(service, 'day-job', day, capture(archive(initial)))
    with factory() as session:
        item = session.scalars(select(Item)).one()
        assert item.status == '待处理'
        assert '重算结果已变化' in item.reason
        assert session.scalar(select(Execution)) is None
    assert adapter.submits == 0



def test_roster_retry_cannot_expand_first_saved_price_authorization(priced_roster, monkeypatch):
    factory, service, adapter, day, _, initial, archive, capture = priced_roster
    original = roster.run_roster_pricing(service, 'day-job', day, capture(archive(initial)))
    key = initial.online[0].key
    update_settings(factory, {'autonomous_enabled': True, 'site_enabled': [key.site], 'pilot_shops': [key.shop_id]})
    changed = initial.model_copy(deep=True)
    for line in changed.lines:
        line.loss_amount -= 1
    monkeypatch.setattr(roster, 'run_workflow', lambda *args, **kwargs: pytest.fail('重试不能扩大原拟价范围'))
    retry = roster.run_roster_pricing(service, 'day-job', day, capture(archive(changed)))
    assert retry[0]['batch_id'] == original[0]['batch_id']
    with factory() as session:
        assert session.scalar(select(Execution)) is None
    assert adapter.submits == 0
