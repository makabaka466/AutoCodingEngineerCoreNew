"""Generic structured-model runtime used by non-development workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

from autocoding_agent.core.models import AgentUsage, RuntimeTurn
from autocoding_agent.ports.runtime import RuntimeEventSink

StructuredOutputT = TypeVar("StructuredOutputT", bound=BaseModel)


@dataclass(frozen=True)
class StructuredRuntimeResult(Generic[StructuredOutputT]):
    """A validated model payload plus resumable runtime metadata."""

    output: StructuredOutputT
    runtime_session_id: str
    usage: AgentUsage


class StructuredRuntime(Protocol):
    def run_structured(
        self,
        turn: RuntimeTurn,
        response_model: type[StructuredOutputT],
    ) -> StructuredRuntimeResult[StructuredOutputT]:
        """Execute one turn and validate it against ``response_model``."""

        ...


class ObservableStructuredRuntime(StructuredRuntime, Protocol):
    def run_structured_observed(
        self,
        turn: RuntimeTurn,
        response_model: type[StructuredOutputT],
        run_id: str,
        event_sink: RuntimeEventSink,
    ) -> StructuredRuntimeResult[StructuredOutputT]:
        """Execute a structured turn while emitting sanitized Runtime activity."""

        ...
