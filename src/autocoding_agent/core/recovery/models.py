"""User-visible recovery choices and scan results."""

from enum import StrEnum

from pydantic import BaseModel, Field


class RecoveryAction(StrEnum):
    READ_ONLY_INSPECT = "read_only_inspect"
    REPLAN = "replan"
    CANCEL = "cancel"


class RecoveryScanResult(BaseModel):
    recovered_task_ids: list[str] = Field(default_factory=list)
    skipped_live_run_ids: list[str] = Field(default_factory=list)
