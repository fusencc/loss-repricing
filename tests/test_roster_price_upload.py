"""历史名单任务仅查询；所有外部请求均为模拟。"""
import json
from datetime import datetime
from uuid import uuid4

import httpx
import pytest

from app.adapters.roster_price_upload import ROSTER_UPLOAD_TRACE, RosterPriceTransport
from app.config import Settings
from app.domain.engine import evaluate
from app.services.store import BEIJING
from fake_adapter import DemoAdapter


@pytest.mark.parametrize('rows,task_id,expected', [
    ([{'id': 'old'}], None, 'unknown'),
    ([{'id': None}], None, 'unknown'),
    ([{'id': 'first'}, {'id': 'second'}], None, 'unknown'),
    ([{'id': 'first'}, {'id': 'second'}], 'second', 'accepted'),
    ([{}], 'different-id', 'unknown'),
    ([{'sellerSkuId': 'other'}], None, 'unknown'),
    ([{'productId': 'other'}], None, 'unknown'),
    ([{'shopName': 'other'}], None, 'unknown'),
    ([{'type': 1}], None, 'unknown'),
    ([{'priceIncludeVat': '999'}], None, 'unknown'),
    ([{'status': 1}], None, 'unknown'),
    ([{'status': 3}], None, 'rejected'),
    ([{'status': 5}], None, 'rejected'),
    ([{'status': 6}], None, 'rejected'),
    ([], None, 'unknown'),
])
def test_historical_roster_task_requires_unique_new_bound_outcome(tmp_path, monkeypatch, rows, task_id, expected):
    from app.adapters.manual_live import ManualLiveAdapter

    snapshot = DemoAdapter(tmp_path).collect(datetime.now(BEIJING).date())
    decision = next(d for d in evaluate(snapshot, snapshot.as_of) if d.ready)
    key = decision.key
    a = {'key': key.model_dump(mode='json'), 'shop_name': '测试店', 'upload_price': str(decision.upload_price)}
    decision.trace[ROSTER_UPLOAD_TRACE] = {'authorization': a}
    execution_id = str(uuid4())
    directory = tmp_path / 'erp-roster-price-executions' / execution_id
    directory.mkdir(parents=True)
    (directory / f'intent-{execution_id}.json').write_text(json.dumps({'authorization': a, 'previous_log_ids': ['old']}))
    log = {'id': 'new', 'type': 2, 'status': 2, 'shopName': a['shop_name'], 'productId': key.product_id,
           'sellerSkuId': key.item_id, 'priceIncludeVat': a['upload_price']}
    monkeypatch.setattr(ManualLiveAdapter, '_logs', lambda self: [log | row for row in rows])
    with httpx.Client(transport=httpx.MockTransport(lambda req: pytest.fail(f'Unexpected request: {req.url.path}'))) as client:
        transport = RosterPriceTransport(Settings(_env_file=None, runtime_dir=tmp_path), client=client)
        assert not hasattr(transport, 'prepare') and not hasattr(transport, 'submit')
        result = transport.lookup(execution_id, task_id, decision)
        assert result.status == expected
        assert result.actual_price is None and result.price_record is None


@pytest.mark.parametrize('state', ['submitting', 'accepted', 'unknown'])
@pytest.mark.parametrize('outcome,expected', [('unknown', None), ('accepted', 'uploaded'), ('rejected', 'failed')])
def test_historical_reconciliation_only_looks_up_and_preserves_unknown_lock(tmp_path, monkeypatch, state, outcome, expected):
    from app.adapters.base import LookupResult
    from app.db import Base, create_database
    from app.models import Batch, Execution, Item, VariantGuard
    from app.services.execution import PricingService
    from app.services.store import stamp

    snapshot = DemoAdapter(tmp_path).collect(datetime.now(BEIJING).date())
    decision = next(d for d in evaluate(snapshot, snapshot.as_of) if d.ready)
    decision.trace[ROSTER_UPLOAD_TRACE] = {'authorization': {'key': decision.key.model_dump(mode='json')}}
    identity = decision.key.target_identity()
    execution_id = str(uuid4())
    engine, factory = create_database(f"sqlite:///{tmp_path / 'history.sqlite'}")
    Base.metadata.create_all(engine)
    try:
        with factory.begin() as session:
            session.add(Batch(id='batch', run_key='historical-roster', business_date=str(snapshot.business_date),
                              mode='live', status='pending_verification', created_at=stamp(), snapshot={}, summary={}))
            session.flush()
            session.add(Item(id='item', batch_id='batch', variant_id=identity, key=decision.key.model_dump(mode='json'),
                             decision=decision.model_dump(mode='json'), status='待核验', phase=state, reason='', updated_at=stamp()))
            session.flush()
            session.add(Execution(id=execution_id, item_id='item', variant_id=identity, state=state,
                                  request=decision.model_dump(mode='json'), response={}, evidence={},
                                  created_at=stamp(), updated_at=stamp()))
            session.add(VariantGuard(variant_id=identity, execution_id=execution_id))
        adapter = DemoAdapter(tmp_path)
        adapter.mode = 'live'
        adapter.submit = lambda *args: pytest.fail('Historical records must never be retransmitted')
        adapter.lookup = lambda *args: pytest.fail('Historical roster must use its task lookup')
        service = PricingService(factory, adapter, Settings(_env_file=None, runtime_dir=tmp_path, coordination_required=False))
        calls = []

        class HistoricalLookup:
            def lookup(self, ident, task_id, stored):
                calls.append(ident)
                assert stored.trace[ROSTER_UPLOAD_TRACE] == decision.trace[ROSTER_UPLOAD_TRACE]
                return LookupResult(outcome, message='mock historical outcome')

        monkeypatch.setattr(service, '_roster_transport', lambda: HistoricalLookup())
        service.reconcile()
        service.reconcile()
        assert calls == [execution_id] * (2 if outcome == 'unknown' else 1)
        with factory() as session:
            assert session.get(Execution, execution_id).state == (expected or state)
            assert session.get(VariantGuard, identity).execution_id == (execution_id if outcome == 'unknown' else None)
    finally:
        engine.dispose()
