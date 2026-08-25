"""Validated task lifecycle transitions for the development Agent."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from autocoding_agent.core.models import AgentEvent, EventType, utc_now
from autocoding_agent.core.state_machine.models import FailureClass, TaskState, TransitionRule


class LifecycleSession(Protocol):
    """Minimum aggregate surface required by the shared lifecycle machine."""

    task_state: TaskState
    version: int
    events: list[AgentEvent]
    updated_at: datetime


class InvalidStateTransition(RuntimeError):
    """Raised when a command attempts an impossible lifecycle transition."""


class StaleTaskVersion(RuntimeError):
    """Raised when a caller acts on an older task snapshot."""


_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset({TaskState.INSPECTING, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.INSPECTING: frozenset(
        {
            TaskState.WAITING_INPUT,
            TaskState.QUERYING_DATA,
            TaskState.WAITING_MODIFY_APPROVAL,
            TaskState.WAITING_VERIFY_APPROVAL,
            TaskState.PAUSED,
            TaskState.RECOVERY_REQUIRED,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.WAITING_INPUT: frozenset(
        {TaskState.INSPECTING, TaskState.PAUSED, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.QUERYING_DATA: frozenset(
        {TaskState.INSPECTING, TaskState.RECOVERY_REQUIRED, TaskState.FAILED}
    ),
    TaskState.WAITING_MODIFY_APPROVAL: frozenset(
        {
            TaskState.IMPLEMENTING,
            TaskState.INSPECTING,
            TaskState.PAUSED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.IMPLEMENTING: frozenset(
        {
            TaskState.WAITING_INPUT,
            TaskState.WAITING_MODIFY_APPROVAL,
            TaskState.WAITING_VERIFY_APPROVAL,
            TaskState.PAUSED,
            TaskState.RECOVERY_REQUIRED,
            TaskState.COMPLETED,
            TaskState.FAILED,
        }
    ),
    TaskState.WAITING_VERIFY_APPROVAL: frozenset(
        {
            TaskState.VERIFYING,
            TaskState.INSPECTING,
            TaskState.PAUSED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.VERIFYING: frozenset(
        {
            TaskState.WAITING_INPUT,
            TaskState.WAITING_MODIFY_APPROVAL,
            TaskState.REPLANNING,
            TaskState.PAUSED,
            TaskState.RECOVERY_REQUIRED,
            TaskState.COMPLETED,
            TaskState.FAILED,
        }
    ),
    TaskState.REPLANNING: frozenset(
        {
            TaskState.INSPECTING,
            TaskState.WAITING_INPUT,
            TaskState.WAITING_MODIFY_APPROVAL,
            TaskState.PAUSED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.PAUSED: frozenset(
        {
            TaskState.INSPECTING,
            TaskState.REPLANNING,
            TaskState.RECOVERY_REQUIRED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.RECOVERY_REQUIRED: frozenset(
        {
            TaskState.INSPECTING,
            TaskState.REPLANNING,
            TaskState.PAUSED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    # FAILED remains reopenable during the JSON-session compatibility phase. A later recovery
    # phase will classify terminal versus recoverable failures before removing this edge.
    TaskState.FAILED: frozenset({TaskState.INSPECTING, TaskState.CANCELLED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


class AgentStateMachine:
    """Own lifecycle mutation and produce one traceable event per real transition."""

    terminal_states = frozenset({TaskState.COMPLETED, TaskState.CANCELLED})
    rules = tuple(TransitionRule(source, targets) for source, targets in _TRANSITIONS.items())

    def allowed_targets(self, state: TaskState) -> frozenset[TaskState]:
        return _TRANSITIONS[state]

    def can_transition(self, current: TaskState, target: TaskState) -> bool:
        return current == target or target in self.allowed_targets(current)

    def is_terminal(self, state: TaskState) -> bool:
        return state in self.terminal_states

    def transition(
        self,
        session: LifecycleSession,
        target: TaskState,
        *,
        reason: str,
        actor: str = "host",
        command_id: str | None = None,
        expected_version: int | None = None,
        failure_class: FailureClass | None = None,
    ) -> AgentEvent | None:
        """Validate and apply one aggregate transition; persistence stays with the Engine."""

        if expected_version is not None and session.version != expected_version:
            raise StaleTaskVersion(
                f"Task version changed: expected {expected_version}, actual {session.version}."
            )
        current = session.task_state
        if current == target:
            return None
        if target not in self.allowed_targets(current):
            raise InvalidStateTransition(
                f"Task cannot transition from {current.value} to {target.value}."
            )
        explanation = " ".join(reason.split()).strip()
        if not explanation:
            raise ValueError("A state transition reason is required.")

        next_version = session.version + 1
        data: dict[str, object] = {
            "from": current.value,
            "to": target.value,
            "reason": explanation,
            "version": next_version,
        }
        if failure_class:
            data["failure_class"] = failure_class.value
        event = AgentEvent(
            type=EventType.STATE_TRANSITIONED,
            message=f"Task state changed from {current.value} to {target.value}.",
            actor=actor,
            command_id=command_id,
            data=data,
        )
        session.task_state = target
        session.version = next_version
        session.events.append(event)
        session.updated_at = utc_now()
        return event
