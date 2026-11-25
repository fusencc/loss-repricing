"""历史过滤日志只落档；不得建立执行意图、锁或调价任务。"""
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from app.models import Batch, Item, Execution, Job, VariantGuard
from app.services.records import department_records
from test_erp_roster_pricing import priced_roster


def scope_archive(service, scope, rows, *, complete=True):
    scope = dict(scope, history_filter=rows, capture_complete=complete,
                 status='completed' if complete else 'blocked')
    directory = service.config.runtime_dir / 'erp-workflows' / uuid4().hex
    directory.mkdir(parents=True)
    scope['artifact_dir'] = str(directory)
    (directory / 'result.json').write_text(json.dumps(scope))
    return scope


def test_history_backfill_deduplicates_full_keys_and_is_idempotent_without_execution(priced_roster):
    from app.services.roster_filter_records import save_roster_filter_records
    factory, service, adapter, day, name, initial, archive = priced_roster
    key = initial.online[0].key.model_dump(mode='json')
    scope = scope_archive(service, archive(initial), [
        {'key': key, 'count': 2, 'status': 'history_protected'},
        {'key': key, 'count': 2, 'status': 'history_protected'},
        {'key': dict(key, sku='UNKNOWN'), 'count': None, 'status': 'history_unknown'},
        {'key': dict(key, sku='PASSED'), 'count': 0, 'status': 'passed'},
    ])
    first = save_roster_filter_records(factory, service.config.runtime_dir, 'daily-job', day, scope)
    assert save_roster_filter_records(factory, service.config.runtime_dir, 'daily-job', day, scope) == first
    with factory() as session:
        items = session.scalars(select(Item)).all()
        assert len(items) == 1 and items[0].status == '本轮不调'
        assert items[0].key == key
        assert items[0].decision['ready'] is False
        assert items[0].decision['trace']['recent_price_filter']['count'] == 2
        assert '近72小时' in items[0].reason
        assert session.scalar(select(func.count()).select_from(Batch)) == 1
        saved_batch = session.get(Batch, first)
        assert set(saved_batch.snapshot) == {'source_versions', 'erp_roster_filter_log'}
        assert saved_batch.snapshot['erp_roster_filter_log']['preview_sha256'] == scope['preview_sha256']
        for model in (Execution, Job, VariantGuard):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        view = department_records(session, 'department:' + scope['department'], mode='live')
        assert view['counts'] == {'本轮不调': 1}
        assert view['items'][0]['seller']['name'] == name
        assert view['scope_issues'] == []
    assert adapter.submits == 0


def test_incomplete_collection_is_a_scope_issue_and_never_a_fake_product_or_zero_history(priced_roster):
    from app.services.roster_filter_records import save_roster_filter_records
    factory, service, _, day, name, initial, archive = priced_roster
    scope = scope_archive(service, archive(initial), [], complete=False)
    save_roster_filter_records(factory, service.config.runtime_dir, 'job', day, scope)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Item)) == 0
        view = department_records(session, 'department:' + scope['department'], mode='live')
        assert view['total'] == 0
        assert view['scope_issues'][0]['salesperson'] == name
        assert '采集未完成' in view['scope_issues'][0]['reason']
        assert view['scope_issues'][0]['business_date'] == day.isoformat()


def test_filter_records_require_original_archive_and_reject_changed_evidence(priced_roster):
    from app.services.roster_filter_records import save_roster_filter_records
    factory, service, _, day, _, initial, archive = priced_roster
    key = initial.online[0].key.model_dump(mode='json')
    scope = scope_archive(service, archive(initial), [{'key': key, 'count': 1, 'status': 'history_protected'}])
    scope['history_filter'] = [{'key': key, 'count': 99, 'status': 'history_protected'}]
    with pytest.raises(ValueError):
        save_roster_filter_records(factory, service.config.runtime_dir, 'job', day, scope)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Batch)) == 0



def test_same_sku_in_different_warehouses_keeps_both_filter_records(priced_roster):
    from app.services.roster_filter_records import save_roster_filter_records
    factory, service, _, day, _, initial, archive = priced_roster
    key = initial.online[0].key.model_dump(mode='json')
    scope = scope_archive(service, archive(initial), [
        {'key': key, 'count': 1, 'status': 'history_protected'},
        {'key': dict(key, warehouse='另一仓库'), 'count': 1, 'status': 'history_protected'},
    ])
    save_roster_filter_records(factory, service.config.runtime_dir, 'job', day, scope)
    with factory() as session:
        items = session.scalars(select(Item)).all()
        assert len(items) == 2
        assert {i.key['warehouse'] for i in items} == {key['warehouse'], '另一仓库'}
