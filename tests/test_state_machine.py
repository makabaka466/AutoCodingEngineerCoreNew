"""Deterministic lifecycle and legacy-session tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from autocoding_agent.core.models import AgentSession, AgentStatus, ApprovalScope, EventType
from autocoding_agent.core.state_machine.machine import (
    AgentStateMachine,
    InvalidStateTransition,
    StaleTaskVersion,
)
from autocoding_agent.core.state_machine.models import (
    AgentCommand,
    AgentCommandType,
    TaskState,
    TransitionRule,
)


def _session(tmp_path: Path) -> AgentSession:
    return AgentSession(workspace=str(tmp_path), goal="Trace the lifecycle.")


def test_transition_records_reason_actor_and_monotonic_version(tmp_path: Path) -> None:
    session = _session(tmp_path)
    machine = AgentStateMachine()

    event = machine.transition(
        session,
        TaskState.INSPECTING,
        reason="Begin the read-only investigation.",
        actor="host",
        command_id="command-1",
        expected_version=0,
    )

    assert event is not None
    assert session.task_state == TaskState.INSPECTING
    assert session.version == 1
    assert session.events == [event]
    assert event.type == EventType.STATE_TRANSITIONED
    assert event.actor == "host"
    assert event.command_id == "command-1"
    assert event.data == {
        "from": "created",
        "to": "inspecting",
        "reason": "Begin the read-only investigation.",
        "version": 1,
    }


def test_same_state_transition_is_idempotent(tmp_path: Path) -> None:
    session = _session(tmp_path)
    machine = AgentStateMachine()
    machine.transition(session, TaskState.INSPECTING, reason="Begin.")
    event_count = len(session.events)
    version = session.version

    event = machine.transition(
        session,
        TaskState.INSPECTING,
        reason="The same runtime phase is still active.",
        expected_version=version,
    )

    assert event is None
    assert session.version == version
    assert len(session.events) == event_count


def test_illegal_transition_is_rejected_without_mutation(tmp_path: Path) -> None:
    session = _session(tmp_path)
    machine = AgentStateMachine()

    with pytest.raises(InvalidStateTransition, match="created to verifying"):
        machine.transition(session, TaskState.VERIFYING, reason="Skip every required phase.")

    assert session.task_state == TaskState.CREATED
    assert session.version == 0
    assert session.events == []


def test_stale_expected_version_is_rejected(tmp_path: Path) -> None:
    session = _session(tmp_path)
    machine = AgentStateMachine()
    machine.transition(session, TaskState.INSPECTING, reason="Begin.")

    with pytest.raises(StaleTaskVersion, match="expected 0, actual 1"):
        machine.transition(
            session,
            TaskState.WAITING_INPUT,
            reason="Need one answer.",
            expected_version=0,
        )

    assert session.task_state == TaskState.INSPECTING
    assert session.version == 1


@pytest.mark.parametrize(
    ("status", "scope", "expected"),
    [
        (None, None, TaskState.CREATED),
        (AgentStatus.NEEDS_INPUT, None, TaskState.WAITING_INPUT),
        (AgentStatus.QUERY_REQUIRED, None, TaskState.QUERYING_DATA),
        (
            AgentStatus.APPROVAL_REQUIRED,
            ApprovalScope.MODIFY,
            TaskState.WAITING_MODIFY_APPROVAL,
        ),
        (
            AgentStatus.APPROVAL_REQUIRED,
            ApprovalScope.VERIFY,
            TaskState.WAITING_VERIFY_APPROVAL,
        ),
        (AgentStatus.COMPLETED, None, TaskState.COMPLETED),
        (AgentStatus.FAILED, None, TaskState.FAILED),
    ],
)
def test_legacy_session_infers_lifecycle_state(
    tmp_path: Path,
    status: AgentStatus | None,
    scope: ApprovalScope | None,
    expected: TaskState,
) -> None:
    payload: dict[str, object] = {
        "workspace": str(tmp_path),
        "goal": "Load an old session.",
        "status": status.value if status else None,
    }
    if scope:
        payload["pending_approval"] = {
            "scope": scope.value,
            "reason": "Legacy request.",
            "proposed_actions": [],
        }

    restored = AgentSession.model_validate(payload)

    assert restored.task_state == expected
    assert restored.version == 0


def test_agent_command_requires_a_nonnegative_expected_version() -> None:
    command = AgentCommand(
        task_id="task-1",
        type=AgentCommandType.SUBMIT_USER_INPUT,
        expected_version=3,
    )
    assert command.expected_version == 3
    assert command.actor == "user"

    with pytest.raises(ValidationError):
        AgentCommand(
            task_id="task-1",
            type=AgentCommandType.SUBMIT_USER_INPUT,
            expected_version=-1,
        )


def test_transition_rules_cover_every_task_state_once() -> None:
    rules = AgentStateMachine.rules

    assert all(isinstance(rule, TransitionRule) for rule in rules)
    assert {rule.source for rule in rules} == set(TaskState)
    assert len({rule.source for rule in rules}) == len(rules)
