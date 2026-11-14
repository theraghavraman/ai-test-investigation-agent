"""Authenticated HTTP API for approved projects, durable jobs, and evidence reports."""
import json
import logging
import shutil
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from redis import Redis
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.db import Investigation, Project, db_session, utcnow
from app.queue import dispatch_job
from app.reports import build_report, render_markdown
from app.schemas import InvestigationCreate, InvestigationView, ProjectView
from app.settings import Settings, get_settings
from app.storage import ArchiveError, extract_approved

log = logging.getLogger(__name__)
app = FastAPI(
    title="AI Code Testing & Bug Investigation Agent",
    version="1.0.0",
    description="Approved-file test generation, bounded Docker pytest, and evidence-separated reports",
    docs_url="/docs", redoc_url=None, openapi_url="/openapi.json",
)


@app.middleware("http")
async def security_and_size_guard(request: Request, call_next):
    settings = get_settings()
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > settings.max_archive_bytes + 65_536:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "invalid Content-Length header"}, status_code=400)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/health/live", include_in_schema=False)
def liveness():
    return {"status": "alive"}


@app.get("/health/ready", include_in_schema=False)
def readiness(db: Session = Depends(db_session), settings: Settings = Depends(get_settings)):
    try:
        db.execute(text("SELECT 1"))
        connection = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            connection.ping()
        finally:
            connection.close()
        return {"status": "ready"}
    except Exception:
        raise HTTPException(status_code=503, detail="Dependencies unavailable") from None


@app.post("/v1/projects", response_model=ProjectView, status_code=201, dependencies=[Depends(require_api_key)])
async def create_project(
    archive: UploadFile = File(...),
    approved_paths: str = Form(..., description='JSON array of exact allowed archive paths'),
    db: Session = Depends(db_session), settings: Settings = Depends(get_settings),
):
    try:
        allowed = json.loads(approved_paths)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="approved_paths must be a JSON array") from None
    if not archive.filename or not archive.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=422, detail="Upload a .zip archive")
    # Reverse proxy MUST also enforce maximum request-body size: UploadFile may
    # have spooled before this read is invoked.
    data = await archive.read(settings.max_archive_bytes + 1)
    await archive.close()
    if len(data) > settings.max_archive_bytes:
        raise HTTPException(status_code=413, detail="archive too large")
    project_id = str(uuid4())
    dest = settings.data_dir / "projects" / project_id
    try:
        manifest, digest = extract_approved(data, allowed, dest, settings)
    except (ArchiveError, TypeError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    project = Project(id=project_id, manifest=manifest, digest=digest)
    try:
        db.add(project)
        db.commit()
        db.refresh(project)
    except Exception:
        db.rollback()
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return project


@app.get("/v1/projects/{project_id}", response_model=ProjectView, dependencies=[Depends(require_api_key)])
def get_project(project_id: UUID, db: Session = Depends(db_session)):
    project = db.get(Project, str(project_id))
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@app.post("/v1/investigations", response_model=InvestigationView, status_code=202,
          dependencies=[Depends(require_api_key)])
def create_investigation(
    payload: InvestigationCreate, db: Session = Depends(db_session),
    idempotency_key: str | None = Header(default=None, max_length=128, alias="Idempotency-Key"),
    settings: Settings = Depends(get_settings),
):
    try:
        project_id = str(UUID(payload.project_id))
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid project_id") from None
    if idempotency_key and (len(idempotency_key) < 8 or not idempotency_key.isascii()):
        raise HTTPException(status_code=422, detail="Idempotency-Key must be ASCII and >= 8 characters")
    try:
        with db.begin():
            if idempotency_key:
                previous = db.scalar(select(Investigation).where(
                    Investigation.idempotency_key == idempotency_key))
                if previous:
                    if previous.project_id != project_id or previous.goal != payload.goal:
                        raise HTTPException(status_code=409, detail="idempotency key reused with different request")
                    return previous
            project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
            if not project:
                raise HTTPException(status_code=404, detail="project not found")
            busy = db.scalar(select(func.count()).select_from(Investigation).where(
                Investigation.project_id == project_id,
                Investigation.status.in_(["queued", "running"]),
            ))
            if busy >= settings.max_queued_per_project:
                raise HTTPException(status_code=429, detail="project queue is full")
            job = Investigation(id=str(uuid4()), project_id=project_id, goal=payload.goal,
                                idempotency_key=idempotency_key, status="queued")
            db.add(job)
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            previous = db.scalar(select(Investigation).where(Investigation.idempotency_key == idempotency_key))
            if previous and previous.project_id == project_id and previous.goal == payload.goal:
                return previous
        raise HTTPException(status_code=409, detail="idempotency collision") from None
    # A temporary broker outage is repaired by the periodic DB reconciliation task.
    try:
        dispatch_job(job.id)
    except Exception:
        log.error("Dispatch unavailable for queued investigation %s; reconciliation will retry", job.id)
    return job


@app.get("/v1/investigations", response_model=list[InvestigationView], dependencies=[Depends(require_api_key)])
def list_investigations(
    project_id: UUID, limit: int = 50, status: str | None = None, db: Session = Depends(db_session),
):
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be 1..100")
    if status is not None and status not in {"queued", "running", "completed", "failed", "cancelled"}:
        raise HTTPException(status_code=422, detail="invalid status filter")
    stmt = select(Investigation).where(Investigation.project_id == str(project_id))
    if status is not None:
        stmt = stmt.where(Investigation.status == status)
    return db.scalars(stmt.order_by(Investigation.created_at.desc()).limit(limit)).all()


@app.get("/v1/diagnostics", dependencies=[Depends(require_api_key)])
def diagnostics(db: Session = Depends(db_session)):
    total_projects = db.scalar(select(func.count()).select_from(Project)) or 0
    rows = db.execute(select(Investigation.status, func.count()).group_by(Investigation.status)).all()
    by_status = {s: 0 for s in ("queued", "running", "completed", "failed", "cancelled")}
    for st, count in rows:
        by_status[st] = count
    return {"projects": total_projects, "investigations_by_status": by_status}


@app.get("/v1/investigations/{job_id}", response_model=InvestigationView,
         dependencies=[Depends(require_api_key)])
def get_investigation(job_id: UUID, db: Session = Depends(db_session)):
    job = db.get(Investigation, str(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="investigation not found")
    return job


@app.post("/v1/investigations/{job_id}/cancel", response_model=InvestigationView,
          dependencies=[Depends(require_api_key)])
def cancel_investigation(
    job_id: UUID, db: Session = Depends(db_session), settings: Settings = Depends(get_settings)
):
    with db.begin():
        job = db.scalar(select(Investigation).where(Investigation.id == str(job_id)).with_for_update())
        if not job:
            raise HTTPException(status_code=404, detail="investigation not found")
        if job.status in ("queued", "running"):
            job.cancel_requested = True
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = utcnow()
                project = db.get(Project, job.project_id)
                if project:
                    job.report = build_report(
                        job_id=job.id, project_id=project.id, digest=project.digest,
                        goal=job.goal, status="cancelled", model=settings.openai_model,
                        attempt=0, tools=None, result=None, error_code="CANCEL_REQUESTED",
                    )
    return job


@app.get("/v1/investigations/{job_id}/report", dependencies=[Depends(require_api_key)])
def get_report(job_id: UUID, db: Session = Depends(db_session)):
    job = db.get(Investigation, str(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="investigation not found")
    if not job.report:
        raise HTTPException(status_code=409, detail="report not yet available")
    return JSONResponse(job.report, headers={"Cache-Control": "no-store"})


@app.get("/v1/investigations/{job_id}/report.md", dependencies=[Depends(require_api_key)])
def get_markdown_report(job_id: UUID, db: Session = Depends(db_session)):
    job = db.get(Investigation, str(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="investigation not found")
    if not job.report:
        raise HTTPException(status_code=409, detail="report not yet available")
    return PlainTextResponse(render_markdown(job.report), media_type="text/markdown",
                             headers={"Cache-Control": "no-store"})
