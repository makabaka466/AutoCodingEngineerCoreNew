"""Read-only query boundary for immutable task decisions."""

from typing import Protocol

from autocoding_agent.core.audit.models import DecisionRecord


class DecisionStore(Protocol):
    def list_decisions(self, task_id: str) -> list[DecisionRecord]: ...
