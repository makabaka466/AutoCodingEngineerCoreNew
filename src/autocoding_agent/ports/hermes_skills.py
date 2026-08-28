"""Port for optional, read-only Hermes skill consultations."""

from __future__ import annotations

from typing import Protocol

from autocoding_agent.core.hermes import (
    HermesSkillRequest,
    HermesSkillResult,
    HermesSkillSummary,
)


class HermesSkillService(Protocol):
    def available_skills(self) -> list[HermesSkillSummary]: ...

    def invoke(self, request: HermesSkillRequest) -> HermesSkillResult: ...
