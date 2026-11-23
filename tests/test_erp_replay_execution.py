"""合成 ERP 预览、人工复核、模拟提交和有效价回查的集成验收。"""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.adapters.base import LookupResult, SubmitResult
from app.api import coordination
from app.config import Settings
from app.domain.types import Snapshot
from app.main import create_app
from app.models import Batch, Execution, Item, Job, Notice, VariantGuard
from app.models.coordination import Correction
from app.services import corrections, execution, store
from app.services.coordination import ensure_demo_ownership
from app.services.erp_capture_store import save_capture
from app.worker import Worker


@pytest.fixture
def replay(tmp_path, monkeypatch):
    fixtures = Path(__file__).parent / "fixtures"
    capture = json.loads((fixtures / "erp-pricing-capture.sample.json").read_text())
    evidence = json.loads((fixtures / "erp-pricing-evidence.sample.json").read_text())
    now = datetime.fromisoformat(evidence["evaluated_at"])
    for module in (coordination, corrections, execution, store):
        monkeypatch.setattr(module, "utcnow", lambda: now)

    config = Settings(
        _env_file=None, environment="development", adapter_mode="demo", coordination_required=True,
        live_submit_enabled=False, ding_enabled=False, notification_mode="demo",
        runtime_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'isolated.sqlite3'}",
        admin_password="isolated-test-password", session_secret="isolated-test-session-secret-12345678",
    )
    app = create_app(config)
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": config.admin_password})
        assert login.status_code == 200
        evidence["capture_id"] = save_capture(tmp_path, capture)["capture_id"]
        yield client, evidence, now


def load_demo_input(client, evidence, now, monkeypatch):
    capture_id = evidence["capture_id"]
    url = f"/api/sources/erp/captures/{capture_id}/pricing-previews"
    response = client.post(url, json=evidence)
    assert response.status_code == 201
    report = response.json()
    assert report["synthetic"] and report["batch_input_available"]
    assert not report["execution_allowed"]
    app = client.app
    path = app.state.config.runtime_dir / "erp-pricing-previews" / (report["preview_id"] + ".json")
    snapshot = Snapshot.model_validate(json.loads(path.read_text())["_snapshot"])
    for online in snapshot.online:
        online.observed_at = now
    assert app.state.adapter.mode == "demo"
    monkeypatch.setattr(app.state.adapter, "collect", lambda day: snapshot.model_copy(deep=True))
    store.update_settings(app.state.factory, {
        "schedule_enabled": False, "autonomous_enabled": False,
        "pilot_shops": ["SAMPLE-MY-01"], "site_enabled": ["MY"],
    })
    return report, snapshot


@pytest.mark.parametrize("price_changed", [False, True], ids=["unknown_then_verified", "stale_after_approval"])
def test_preview_manual_review_and_simulated_reconciliation(replay, monkeypatch, price_changed):
    client, evidence, now = replay
    app = client.app
    report, snapshot = load_demo_input(client, evidence, now, monkeypatch)
    preview = next(row for row in report["decisions"] if row["ready"])
    assert Decimal(preview["target_actual"]) == Decimal("18.72")
    assert Decimal(preview["upload_price"]) == Decimal("20.80")

    sent, looked_up = [], []
    receipt_visible = False

    def submit(execution_id, decision):
        assert Decimal(decision.upload_price) == Decimal(preview["upload_price"])
        sent.append(execution_id)
        raise TimeoutError("模拟外部受理后连接中断")

    def lookup(execution_id, task_id, key):
        assert execution_id in sent and key.item_id == "0001001"
        looked_up.append(execution_id)
        if not receipt_visible:
            return LookupResult("unknown")
        return LookupResult("accepted", Decimal("18.72"), now, now)

    monkeypatch.setattr(app.state.adapter, "submit", submit)
    monkeypatch.setattr(app.state.adapter, "lookup", lookup)
    factory = app.state.factory
    worker = Worker(factory, app.state.service)
    run = client.post("/api/batches/run")
    assert run.status_code == 200
    worker.tick()
    job = client.get(f"/api/jobs/{run.json()['job_id']}").json()
    assert job["state"] == "done"
    batch_id = job["batch_id"]
    with factory() as session:
        item = next(row for row in session.scalars(select(Item).where(Item.batch_id == batch_id))
                    if row.key == preview["key"])
        item_id = item.id
        assert item.decision["selected_row_id"] == preview["selected_row_id"]
        assert item.decision["upload_price"] == preview["upload_price"]
        assert not session.scalars(select(Execution)).all()

    detail = client.get(f"/api/items/{item_id}")
    assert detail.status_code == 200
    version = detail.json()["control"]["version"]
    proposal = client.post(f"/api/items/{item_id}/corrections", json={
        "desired_actual": preview["target_actual"], "reason": "复核合成 ERP 预览", "version": version,
    })
    assert proposal.status_code == 200, proposal.text
    proposal = proposal.json()
    worker.tick()
    assert sent == []

    url = f"/api/corrections/{proposal['id']}/approve"
    body = {"version": proposal["control_version"]}
    approved = client.post(url, json=body)
    assert approved.status_code == 200, approved.text
    approved = approved.json()
    assert client.post(url, json=body).json()["job_id"] == approved["job_id"]
    if price_changed:
        snapshot.online[0].retail_price = Decimal("21.00")
    worker.tick()

    with factory() as session:
        correction = session.get(Correction, proposal["id"])
        assert session.get(Job, approved["job_id"]).state == "done"
        if price_changed:
            assert correction.state == "stale"
            assert "变化" in correction.result["message"]
            assert sent == looked_up == []
            assert not session.scalars(select(Execution)).all()
            return
        assert correction.state == "executing"
        attempt = session.get(Execution, correction.execution_id)
        assert attempt.state == "unknown"
        assert session.get(VariantGuard, attempt.variant_id).execution_id == attempt.id
        attempt_id = attempt.id

    restarted = execution.PricingService(factory, app.state.adapter, app.state.config)
    # 同一修正任务由另一服务实例再次处理时，只回查已保存的执行。
    corrections.run_correction(restarted, proposal["id"])
    assert sent == [attempt_id]
    assert looked_up == [attempt_id, attempt_id]
    receipt_visible = True
    from app.services import notifications

    write = notifications.write_batch_notices

    def interrupt(session, batch_id, **options):
        write(session, batch_id, **options)
        raise RuntimeError("interrupt before notification commit")

    with monkeypatch.context() as patch:
        patch.setattr(notifications, "write_batch_notices", interrupt)
        with pytest.raises(RuntimeError, match="interrupt before notification commit"):
            restarted.reconcile()
    with factory() as session:
        attempt = session.get(Execution, attempt_id)
        assert attempt.state == "unknown"
        assert session.get(VariantGuard, attempt.variant_id).execution_id == attempt_id
    restarted.reconcile()
    restarted.reconcile()
    with factory() as session:
        correction = session.get(Correction, proposal["id"])
        attempt = session.get(Execution, attempt_id)
        assert correction.state == attempt.state == "verified"
        assert attempt.evidence["actual_price"] == "18.72"
        assert attempt.evidence["mode"] == "demo"
        assert session.get(VariantGuard, attempt.variant_id).execution_id is None
    assert sent == [attempt_id]
    assert looked_up == [attempt_id] * 4

    # 独立调度把已核验的模拟提价汇总送到网页收件箱，确认状态独立存储。
    assert client.post("/api/notifications/dispatch").status_code == 200
    with factory() as session:
        correction_item = session.get(Item, attempt.item_id)
        notice = session.scalars(select(Notice).where(Notice.batch_id == correction_item.batch_id)).one()
        assert notice.synthetic and notice.delivery_state == "demo" and notice.task_id is None
        assert notice.summary["statuses"] == {"已核验": 1}
        assert notice.ack_at is None
        notice_id = notice.id
    assert client.post(f"/api/notifications/{notice_id}/ack").status_code == 200
    with factory() as session:
        notice = session.get(Notice, notice_id)
        assert notice.ack_by == "admin"
        assert notice.delivery_state == "demo" and notice.ack_at is not None


def test_preview_batch_preserves_partial_failure_and_independent_verification(replay, monkeypatch):
    client, evidence, now = replay
    app = client.app
    factory = app.state.factory
    report, snapshot = load_demo_input(client, evidence, now, monkeypatch)
    expected = {row["key"]["item_id"]: row for row in report["decisions"] if row["ready"]}
    assert set(expected) == {"0001001", "0001002"}
    ensure_demo_ownership(factory, [row.key for row in snapshot.online])
    store.update_settings(factory, {"autonomous_enabled": True})
    collected, sent, observed = [], [], []

    def collect(day):
        assert day == snapshot.business_date
        collected.append(day)
        return snapshot.model_copy(deep=True)

    def submit(execution_id, decision):
        assert len(collected) == 2
        item_id = decision.key.item_id
        sent.append(item_id)
        assert decision.upload_price == Decimal(expected[item_id]["upload_price"])
        if len(sent) == 1:
            return SubmitResult("accepted", "simulated-task")
        with factory() as session:
            assert session.scalars(select(Batch)).one().status == "running"
            assert session.scalars(select(Notice)).first() is None
        return SubmitResult("rejected", message="模拟逐行拒绝")

    def lookup(execution_id, task_id, key):
        assert task_id == "simulated-task" and key.item_id == sent[0]
        observed.append(key.item_id)
        actual_price = {"0001001": Decimal("18.72"), "0001002": Decimal("33.00")}
        return LookupResult("accepted", actual_price[key.item_id], now, now)

    monkeypatch.setattr(app.state.adapter, "collect", collect)
    monkeypatch.setattr(app.state.adapter, "submit", submit)
    monkeypatch.setattr(app.state.adapter, "lookup", lookup)
    run = client.post("/api/batches/run")
    assert run.status_code == 200
    job_id = run.json()["job_id"]
    Worker(factory, app.state.service).tick()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "done"
    batch_id = job["batch_id"]
    detail = client.get(f"/api/batches/{batch_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert sorted(sent) == ["0001001", "0001002"]
    assert observed == sent[:1]
    with factory() as session:
        rows = {item.key["item_id"]: attempt for item, attempt in session.execute(
            select(Item, Execution).join(Execution, Execution.item_id == Item.id).where(Item.batch_id == batch_id)
        )}
        assert rows[sent[0]].state == "verified"
        assert Decimal(rows[sent[0]].evidence["actual_price"]) == Decimal(expected[sent[0]]["target_actual"])
        assert rows[sent[1]].state == "failed"
        assert rows[sent[1]].response["status"] == "rejected"
        assert all(session.get(VariantGuard, attempt.variant_id).execution_id is None for attempt in rows.values())
        notice = session.scalars(select(Notice).where(Notice.batch_id == batch_id)).one()
        assert notice.delivery_state == "pending"
        assert notice.summary["statuses"] == {"已核验": 1, "待处理": 1}
