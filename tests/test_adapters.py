from datetime import datetime, timezone
from decimal import Decimal
from app.adapters.demo import DemoAdapter
from app.domain.types import Snapshot


def test_demo_has_complete_source_and_persistent_effective_receipt(tmp_path):
    now = datetime.now(timezone.utc)
    adapter = DemoAdapter(tmp_path)
    snapshot = adapter.collect(now.astimezone(adapter.timezone).date())
    assert isinstance(snapshot, Snapshot)
    from app.domain.engine import evaluate
    decisions = evaluate(snapshot, datetime.now(timezone.utc))
    ready = [d for d in decisions if d.ready]
    assert len(ready) >= 2
    d = ready[0]
    response = adapter.submit("exec-one", d)
    assert response.status == "accepted"
    recreated = DemoAdapter(tmp_path)
    result = recreated.lookup("exec-one", response.task_id, d.key)
    assert result.status == "accepted" and result.actual_price == d.upload_price * d.discount


def test_demo_unknown_execution_does_not_invent_success(tmp_path):
    adapter = DemoAdapter(tmp_path)
    snapshot = adapter.collect(datetime.now(timezone.utc).astimezone(adapter.timezone).date())
    assert adapter.lookup("missing", None, snapshot.online[0].key).status == "unknown"
