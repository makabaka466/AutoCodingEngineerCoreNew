"""Explainable model-decision records, distinct from host-verified execution facts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from autocoding_agent.core.artifacts.models import ArtifactRecord


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EvidenceRef(BaseModel):
    path: str | None = None
    symbol: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    summary: str
    content_sha256: str | None = None
    git_commit: str | None = None
    verified_by_host: bool = False


class DecisionRecord(BaseModel):
    """Why the model proposed an action; it is not proof that the action occurred."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    event_id: str
    decision_type: str
    summary: str
    reason: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    actor: str = "model"
    model: str
    runtime_session_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChangeExplanation(BaseModel):
    task_id: str
    path: str
    decisions: list[DecisionRecord] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    summary: str
