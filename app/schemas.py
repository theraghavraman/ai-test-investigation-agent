"""Public contracts and deliberately separate model analysis contract."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InvestigationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=36, max_length=36)
    goal: str = Field(min_length=10, max_length=2_000)


class InvestigationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    status: str
    attempts: int
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None


class ProjectView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    manifest: list
    digest: str


class AgentConclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    suspected_causes: list[str]
    recommendations: list[str]
    limitations: list[str]


class TestCaseEvidence(BaseModel):
    name: str
    outcome: Literal["passed", "failed", "error", "skipped"]
    seconds: float = 0.0
    details: str | None = None
