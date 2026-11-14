"""Durable Celery worker with claims, leases, recovery, cancellation, and retention."""
import logging
import shutil
from datetime import timedelta

from celery.exceptions import SoftTimeLimitExceeded
from openai import OpenAIError
from sqlalchemy import select

from app.agent import InvestigationCancelled, InvestigationExpired, investigate
from app.db import Investigation, Project, new_session, utcnow
from app.queue import celery_app, dispatch_job
from app.reports import build_report, save_reports
from app.runner import RunnerError
from app.settings import get_settings
from app.storage import ArchiveError, create_attempt_workspace
from app.tools import Toolset

log = logging.getLogger(__name__)


def claim(job_id: str) -> dict | None:
    settings = get_settings()
    with new_session() as db, db.begin():
        row = db.scalar(select(Investigation).where(Investigation.id == job_id).with_for_update())
        if row is None or row.status != "queued":
            return None
        if row.cancel_requested:
            row.status = "cancelled"
            row.finished_at = utcnow()
            return None
        project = db.get(Project, row.project_id)
        if project is None:
            row.status = "failed"
            row.error_code = "MISSING_PROJECT"
            row.finished_at = utcnow()
            return None
        row.status = "running"
        row.attempts += 1
        row.started_at = utcnow()
        row.lease_until = utcnow() + timedelta(seconds=settings.investigation_timeout_seconds + 120)
        return {
            "id": row.id, "project_id": project.id, "digest": project.digest,
            "manifest": list(project.manifest), "goal": row.goal, "attempt": row.attempts,
        }


def _cancel_check(job_id: str) -> bool:
    with new_session() as db:
        job = db.get(Investigation, job_id)
        return job is None or job.cancel_requested or job.status != "running"


def finish(job_id: str, report: dict, status: str, error_code: str | None) -> bool:
    with new_session() as db, db.begin():
        row = db.scalar(select(Investigation).where(Investigation.id == job_id).with_for_update())
        if row is None or row.status != "running":
            return False
        if row.cancel_requested:
            status = "cancelled"
            report["status"] = "cancelled"
            error_code = error_code or "CANCEL_REQUESTED"
            report["error_code"] = error_code
        row.status = status
        row.finished_at = utcnow()
        row.lease_until = None
        row.error_code = error_code
        row.report = report
    return True


@celery_app.task(name="agent.process", acks_late=True)
def process_investigation(job_id: str) -> None:
    settings = get_settings()
    item = claim(job_id)
    if not item:
        return
    tools = None
    result = None
    status = "completed"
    error_code = None
    try:
        workspace, artifacts, attempt_root = create_attempt_workspace(
            settings.data_dir, item["project_id"], job_id, item["attempt"], item["manifest"]
        )
        tools = Toolset(workspace, artifacts, attempt_root / "audit.jsonl", item["manifest"], settings)
        result = investigate(settings, tools, item["goal"], cancel_check=lambda: _cancel_check(job_id))
        if _cancel_check(job_id):
            status = "cancelled"
    except InvestigationCancelled:
        status = "cancelled"
        error_code = "CANCEL_REQUESTED"
    except InvestigationExpired:
        status = "failed"
        error_code = "INVESTIGATION_TIMEOUT"
    except SoftTimeLimitExceeded:
        status = "failed"
        error_code = "WORKER_TIME_LIMIT"
    except ArchiveError:
        status = "failed"
        error_code = "SNAPSHOT_INTEGRITY_ERROR"
    except RunnerError:
        status = "failed"
        error_code = "DOCKER_RUNNER_ERROR"
    except OpenAIError as exc:
        status = "failed"
        error_code = "MODEL_API_ERROR"
        log.warning("Model API failure for investigation %s: %s", job_id, type(exc).__name__)
    except Exception as exc:
        status = "failed"
        error_code = "INTERNAL_ERROR"
        # Log only the exception TYPE: exception text may contain private source code.
        log.error("Investigation %s failed with %s", job_id, type(exc).__name__)
    report = build_report(
        job_id=job_id, project_id=item["project_id"], digest=item["digest"],
        goal=item["goal"], status=status, model=settings.openai_model,
        attempt=item["attempt"], tools=tools, result=result, error_code=error_code,
    )
    # DB is the authoritative report; save the final cancellation-adjusted
    # report only AFTER the DB transaction commits to avoid status divergence.
    if finish(job_id, report, status, error_code):
        try:
            save_reports(settings.data_dir, job_id, report)
        except OSError:
            log.error("Report file persistence failed for investigation %s", job_id)


@celery_app.task(name="agent.reconcile")
def reconcile() -> dict:
    """Beat repairs lost dispatches and retries crashed worker attempts (max 3)."""
    now = utcnow()
    redispatch = []
    with new_session() as db, db.begin():
        # PostgreSQL row locks allow safe reconciliation even with multiple beat instances.
        items = db.scalars(
            select(Investigation).where(Investigation.status.in_(["queued", "running"]))
            .with_for_update(skip_locked=True).limit(300)
        ).all()
        for item in items:
            if item.cancel_requested:
                if item.status == "queued" or (item.lease_until and item.lease_until < now):
                    item.status = "cancelled"
                    item.finished_at = now
                    project = db.get(Project, item.project_id)
                    if project:
                        item.report = build_report(
                            job_id=item.id, project_id=item.project_id, digest=project.digest,
                            goal=item.goal, status="cancelled", model=get_settings().openai_model,
                            attempt=item.attempts, tools=None, result=None,
                            error_code="CANCEL_REQUESTED",
                        )
                continue
            if item.status == "running" and item.lease_until and item.lease_until < now:
                if item.attempts >= 3:
                    item.status = "failed"
                    item.error_code = "RETRY_LIMIT_REACHED"
                    item.finished_at = now
                    project = db.get(Project, item.project_id)
                    if project:
                        item.report = build_report(
                            job_id=item.id, project_id=item.project_id, digest=project.digest,
                            goal=item.goal, status="failed", model=get_settings().openai_model,
                            attempt=item.attempts, tools=None, result=None,
                            error_code="RETRY_LIMIT_REACHED",
                        )
                else:
                    item.status = "queued"
                    item.lease_until = None
                    redispatch.append(item.id)
            elif item.status == "queued" and item.created_at < now - timedelta(seconds=30):
                redispatch.append(item.id)
    for job_id in redispatch:
        try:
            dispatch_job(job_id)
        except Exception:
            log.error("Job enqueue failed during reconciliation: %s", job_id)
    return {"redispatched": len(redispatch)}


@celery_app.task(name="agent.cleanup")
def cleanup_old_investigations() -> dict:
    """Delete expired investigation artifacts and DB rows; preserve source projects."""
    settings = get_settings()
    cutoff = utcnow() - timedelta(days=settings.report_retention_days)
    expired = []
    with new_session() as db, db.begin():
        items = db.scalars(
            select(Investigation).where(
                Investigation.status.in_(["completed", "failed", "cancelled"]),
                Investigation.finished_at < cutoff,
            ).with_for_update(skip_locked=True).limit(100)
        ).all()
        for item in items:
            try:
                shutil.rmtree(settings.data_dir / "investigations" / item.id)
            except FileNotFoundError:
                pass
            except OSError:
                log.error("Old job artifact cleanup failed: %s", item.id)
                continue  # Preserve DB record so subsequent cleanup can retry.
            expired.append(item.id)
            db.delete(item)
    return {"deleted": len(expired)}
