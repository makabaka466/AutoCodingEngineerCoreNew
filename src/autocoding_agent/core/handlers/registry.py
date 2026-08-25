"""State-to-handler lookup with duplicate and missing-state protection."""

from __future__ import annotations

from collections.abc import Iterable

from autocoding_agent.core.handlers.base import StateHandler
from autocoding_agent.core.state_machine.models import TaskState


class HandlerRegistry:
    def __init__(self, handlers: Iterable[StateHandler]) -> None:
        self._handlers: dict[TaskState, StateHandler] = {}
        for handler in handlers:
            if handler.state in self._handlers:
                raise ValueError(f"Duplicate handler for state {handler.state.value}.")
            self._handlers[handler.state] = handler

    def for_state(self, state: TaskState) -> StateHandler:
        try:
            return self._handlers[state]
        except KeyError as exc:
            raise ValueError(f"Task state {state.value} has no executable handler.") from exc
