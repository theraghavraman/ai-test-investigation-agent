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
