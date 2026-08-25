"""Model runtime ports."""

from collections.abc import Callable
from typing import Protocol

from autocoding_agent.core.models import RuntimeResult, RuntimeTurn
from autocoding_agent.core.runtime.models import RuntimeActivity

RuntimeEventSink = Callable[[RuntimeActivity], None]


class RuntimeInterruptedError(RuntimeError):
    """The host intentionally interrupted an active Runtime process."""


class AgentRuntime(Protocol):
    def run(self, turn: RuntimeTurn) -> RuntimeResult:
        """Execute one model turn and return a validated decision."""

        ...


class ObservableAgentRuntime(AgentRuntime, Protocol):
    def run_observed(
        self,
        turn: RuntimeTurn,
        run_id: str,
        event_sink: RuntimeEventSink,
    ) -> RuntimeResult: ...

    def interrupt(self, run_id: str) -> bool: ...
