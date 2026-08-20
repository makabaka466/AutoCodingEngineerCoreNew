"""Model runtime port."""

from typing import Protocol

from autocoding_agent.core.models import RuntimeResult, RuntimeTurn


class AgentRuntime(Protocol):
    def run(self, turn: RuntimeTurn) -> RuntimeResult:
        """Execute one model turn and return a validated decision."""

        ...
