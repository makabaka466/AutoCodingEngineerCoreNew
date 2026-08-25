"""Task artifact metadata; content is stored outside the target workspace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ArtifactType(StrEnum):
    ANALYSIS = "analysis"
    CONTEXT = "context"
    PROPOSAL = "proposal"
    BASELINE_STATUS = "baseline_status"
    BASELINE_PATCH = "baseline_patch"
    CHANGES_PATCH = "changes_patch"
    TEST_RESULT = "test_result"
    RECOVERY_REPORT = "recovery_report"
    FINAL_REPORT = "final_report"


class ArtifactRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    event_id: str
    type: ArtifactType
    relative_path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    schema_version: int = Field(default=1, ge=1)
    source: str
    host_verified: bool = False
    sensitive: bool = False
    related_paths: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class WorkspaceSnapshot:
    is_git: bool
    dirty: bool
    status_entries: tuple[str, ...]
    related_paths: tuple[str, ...]
    patch: str
    git_commit: str | None
    truncated: bool = False
    error: str | None = None

    def status_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "is_git": self.is_git,
            "dirty": self.dirty,
            "status_entries": list(self.status_entries),
            "related_paths": list(self.related_paths),
            "git_commit": self.git_commit,
            "patch_truncated": self.truncated,
            "error": self.error,
        }
