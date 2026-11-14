"""FastAPI/SQLAlchemy integration tests. Require runtime broker dependencies."""
import io
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base, db_session
from app.settings import get_settings


@pytest.fixture
def client(settings, tmp_path, monkeypatch):
    pytest.importorskip("redis")
    pytest.importorskip("celery")
    from app.api import app

    settings.database_url = f"sqlite:///{tmp_path / 'unit.db'}"
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    def override_session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[db_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr("app.api.dispatch_job", lambda job_id: None)
    counts: dict[str, int] = {}

    def fake_hit(_settings, key, _ttl):
        client_key = key.rsplit(":", 1)[0]  # ignore the window number so tests never straddle a minute
        counts[client_key] = counts.get(client_key, 0) + 1
        return counts[client_key]

    monkeypatch.setattr("app.ratelimit.hit", fake_hit)
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def demo_zip():
    output = io.BytesIO()
    with ZipFile(output, "w") as zf:
        zf.writestr("calculator.py", "def add(a,b): return a+b\n")
    return output.getvalue()


def test_authenticated_project_and_idempotent_job(client, settings):
    assert client.get("/v1/investigations", params={"project_id": "00000000-0000-0000-0000-000000000000"}).status_code == 401
    token = {"Authorization": f"Bearer {settings.api_key.get_secret_value()}"}
    result = client.post("/v1/projects", headers=token, data={"approved_paths": '["calculator.py"]'},
                         files={"archive": ("calculator.zip", demo_zip(), "application/zip")})
    assert result.status_code == 201, result.text
    project_id = result.json()["id"]
    payload = {"project_id": project_id, "goal": "Investigate basic addition behavior"}
    created = client.post("/v1/investigations", headers={**token, "Idempotency-Key": "job-client-123"}, json=payload)
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]
    replay = client.post("/v1/investigations", headers={**token, "Idempotency-Key": "job-client-123"}, json=payload)
    assert replay.status_code == 202 and replay.json()["id"] == job_id
    assert client.get(f"/v1/investigations/{job_id}/report", headers=token).status_code == 409
    assert client.post(f"/v1/investigations/{job_id}/cancel", headers=token).json()["status"] == "cancelled"
    assert client.get(f"/v1/investigations/{job_id}/report", headers=token).json()["status"] == "cancelled"


def test_status_filter_and_diagnostics(client, settings):
    token = {"Authorization": f"Bearer {settings.api_key.get_secret_value()}"}
    res = client.post("/v1/projects", headers=token, data={"approved_paths": '["calculator.py"]'},
                      files={"archive": ("calculator.zip", demo_zip(), "application/zip")})
    project_id = res.json()["id"]
    client.post("/v1/investigations", headers=token, json={"project_id": project_id, "goal": "Check addition behavior"})
    listed = client.get("/v1/investigations", params={"project_id": project_id, "status": "queued"}, headers=token)
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert client.get("/v1/investigations", params={"project_id": project_id, "status": "invalid"}, headers=token).status_code == 422
    diag = client.get("/v1/diagnostics", headers=token)
    assert diag.status_code == 200 and diag.json()["projects"] >= 1
    assert diag.headers.get("X-Content-Type-Options") == "nosniff"


def test_docs_and_schema_disabled_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_rate_limit_counts_failed_auth_and_sets_retry_after(client, settings):
    settings.rate_limit_per_minute = 3
    params = {"project_id": "00000000-0000-0000-0000-000000000000"}
    codes = [client.get("/v1/investigations", params=params).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    limited = client.get("/v1/investigations", params=params,
                         headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"})
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    assert client.get("/health/live").status_code == 200  # health checks are never throttled


def test_rate_limit_disabled_and_fails_open(client, settings, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    params = {"project_id": "00000000-0000-0000-0000-000000000000"}
    settings.rate_limit_per_minute = 0
    assert all(client.get("/v1/investigations", params=params).status_code == 401 for _ in range(5))

    def broken(*_args):
        raise RedisConnectionError("down")

    settings.rate_limit_per_minute = 1
    monkeypatch.setattr("app.ratelimit.hit", broken)
    assert all(client.get("/v1/investigations", params=params).status_code == 401 for _ in range(3))
