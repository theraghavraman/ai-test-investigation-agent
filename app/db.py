"""Persistence. Production tables are created via Alembic, not on web startup."""
from collections.abc import Generator
from datetime import datetime, timezone
from functools import lru_cache
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from app.settings import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    manifest: Mapped[list] = mapped_column(JSON, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    investigations: Mapped[list["Investigation"]] = relationship(back_populates="project")


class Investigation(Base):
    __tablename__ = "investigations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    project: Mapped[Project] = relationship(back_populates="investigations")


@lru_cache
def get_engine():  # type: ignore[unindent]
    return create_engine(get_settings().database_url, pool_pre_ping=True, pool_recycle=1800)


def new_session() -> Session:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)()


def db_session() -> Generator[Session, None, None]:
    with new_session() as db:
        yield db
