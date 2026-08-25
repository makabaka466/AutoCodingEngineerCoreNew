"""Append-only task-event query port."""

from typing import Protocol

from autocoding_agent.core.models import AgentEvent
from autocoding_agent.core.state_machine.models import TaskState


class EventStore(Protocol):
    def list_events(self, task_id: str) -> list[AgentEvent]: ...

    def replay_task_state(self, task_id: str) -> TaskState: ...
