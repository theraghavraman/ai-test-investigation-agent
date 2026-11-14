"""Celery broker and simple durable job dispatcher."""
from celery import Celery

from app.settings import get_settings

settings = get_settings()
celery_app = Celery("agent", broker=settings.redis_url, backend=None, include=["app.worker"])
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_backend=None,
    task_track_started=False, worker_prefetch_multiplier=1,
    task_acks_late=True, task_reject_on_worker_lost=True,
    task_time_limit=1500, task_soft_time_limit=1400,
    broker_connection_retry_on_startup=True,
    task_default_queue="investigations",
    beat_schedule={
        "reconcile-jobs": {"task": "agent.reconcile", "schedule": 60.0},
        "expire-old-reports": {"task": "agent.cleanup", "schedule": 86_400.0},
    },
)


def dispatch_job(job_id: str) -> None:
    celery_app.send_task("agent.process", args=[job_id], queue="investigations")
