import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    config = Settings(database_url=f"sqlite:///{tmp_path/'api.db'}", runtime_dir=tmp_path, coordination_required=False,
        admin_password="test-local-password", session_secret="test-session-secret-32-characters-long", _env_file=None)
    with TestClient(create_app(config)) as client:
        yield client


def login(client):
    result = client.post("/api/auth/login", json={"username":"admin","password":"test-local-password"})
    assert result.status_code == 200


def test_admin_required_and_wrong_password_rejected(client):
    assert client.get("/api/overview").status_code == 401
    assert client.post("/api/auth/login", json={"username":"admin","password":"wrong"}).status_code == 401
    login(client)
    result = client.get("/api/overview")
    assert result.status_code == 200
    assert result.json()["mode"] == "live"
    assert "session_secret" not in result.text and "test-local-password" not in result.text


def test_settings_protections_and_job_contract(client):
    login(client)
    updated = client.patch("/api/settings", json={"pilot_shops":["DEMO-MY"],"site_enabled":["MY"]})
    assert updated.json()["pilot_shops"] == ["DEMO-MY"]
    created = client.post("/api/protections", json={"shop_id":"DEMO-MY","site":"MY","main_sku":"M1","reason":"在途备货"})
    assert created.status_code == 200
    protection_id = created.json()["id"]
    assert client.get("/api/protections").json()[0]["active"] is True
    assert client.delete(f"/api/protections/{protection_id}").status_code == 200
    assert client.get("/api/protections").json() == []
    job = client.post("/api/batches/run").json()
    assert client.get(f"/api/jobs/{job['job_id']}").json()["state"] == "queued"


def test_cross_origin_write_and_unknown_setting_rejected(client):
    login(client)
    assert client.patch("/api/settings", json={"paused":True}, headers={"origin":"https://attacker.example"}).status_code == 403
    assert client.patch("/api/settings", json={"live_submit_enabled":True}).status_code == 422


def test_logout_invalidates_browser_cookie(client):
    login(client)
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401

def test_demo_entry_removed(client):
    login(client)
    assert client.post("/api/demo/run").status_code == 404
    with pytest.raises(ValueError):
        Settings(_env_file=None, adapter_mode="demo")

def test_default_live_storage_is_separate_from_file_reconciliation(tmp_path):
    live = Settings(_env_file=None, runtime_dir=tmp_path)
    file = Settings(_env_file=None, runtime_dir=tmp_path, adapter_mode="file")
    assert live.database_url == f"sqlite:///{tmp_path / 'live-history.db'}"
    assert file.database_url == f"sqlite:///{tmp_path / 'file-reconciliation.db'}"
    assert not live.live_submit_enabled and not live.ding_enabled
    with pytest.raises(ValueError):
        Settings(_env_file=None, notification_mode="demo")
