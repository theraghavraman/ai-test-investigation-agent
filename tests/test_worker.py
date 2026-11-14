"""Durability and report lifecycle integration (requires runtime broker deps)."""
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base, Investigation, Project
from app.storage import extract_approved
from tests.conftest import archive_for


def test_worker_claims_and_persists_structured_report(tmp_path, settings, monkeypatch):
    pytest.importorskip("celery")
    pytest.importorskip("redis")
    pytest.importorskip("openai")
    from app import worker

    settings.database_url = f"sqlite:///{tmp_path / 'worker.db'}"
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    project_id, job_id = str(uuid4()), str(uuid4())
    manifest, sha = extract_approved(archive_for({"calc.py": "def twice(x): return x * 2\n"}),
                                     ["calc.py"], tmp_path / "projects" / project_id, settings)
    with Session(engine) as db, db.begin():
        db.add(Project(id=project_id, manifest=manifest, digest=sha))
        db.add(Investigation(id=job_id, project_id=project_id, goal="Test the twice function in calc.py", status="queued"))

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "new_session", lambda: Session(engine))

    def fake_investigate(settings, tools, objective, cancel_check):
        assert "twice" in tools.read_file("calc.py", 0, 200)["content"]
        tools.write_test("from calc import twice\n\ndef test_twice():\n    assert twice(2) == 4\n")
        return {"analysis": {"summary": "No executed Pytest evidence was provided.",
                             "suspected_causes": [], "recommendations": ["Run tests in Docker"],
                             "limitations": ["Model call was stubbed for unit testing"]},
                "rounds": 1, "tool_calls": 2}

    monkeypatch.setattr(worker, "investigate", fake_investigate)
    worker.process_investigation(job_id)
    with Session(engine) as db:
        job = db.get(Investigation, job_id)
        assert job.status == "completed" and job.attempts == 1
        assert job.report["status"] == "completed"
        assert len(job.report["generated_tests"]) == 1
        assert job.report["test_evidence"]["runs"] == []
    assert (tmp_path / "investigations" / job_id / "report.json").is_file()
    engine.dispose()
