"""State handler registry and permission-bound RuntimeTurn tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from autocoding_agent.core.handlers import (
    HandlerContext,
    HandlerRegistry,
    ImplementHandler,
    InspectHandler,
    RecoveryHandler,
    RecoveryHandlerUnavailable,
    VerifyHandler,
)
from autocoding_agent.core.models import AgentDecision, AgentStatus, RuntimeResult, RuntimeTurn
from autocoding_agent.core.policies import ExecutionPolicy
from autocoding_agent.core.state_machine.models import TaskState


class RecordingRuntime:
    def __init__(self) -> None:
        self.turn: RuntimeTurn | None = None

    def run(self, turn: RuntimeTurn) -> RuntimeResult:
        self.turn = turn
        return RuntimeResult(
            decision=AgentDecision(status=AgentStatus.COMPLETED, message="Done."),
            runtime_session_id="runtime-handler",
        )


def _context(tmp_path: Path) -> HandlerContext:
    return HandlerContext(
        session_id="task",
        runtime_session_id=None,
        workspace=str(tmp_path),
        user_message="Inspect this.",
        history=(),
        system_prompt="Follow policy.",
        capability_dir=None,
    )


def test_registry_selects_handler_and_builds_permission_bound_turn(tmp_path: Path) -> None:
    runtime = RecordingRuntime()
    policy = ExecutionPolicy()
    registry = HandlerRegistry(
        [
            InspectHandler(runtime, policy),
            ImplementHandler(runtime, policy),
            VerifyHandler(runtime, policy),
        ]
    )

    result = registry.for_state(TaskState.IMPLEMENTING).execute(_context(tmp_path))

    assert result.turn.mode.value == "implement"
    assert "Edit" in result.turn.tools
    assert runtime.turn == result.turn


def test_registry_rejects_duplicate_or_missing_handler(tmp_path: Path) -> None:
    runtime = RecordingRuntime()
    policy = ExecutionPolicy()

    with pytest.raises(ValueError, match="Duplicate handler"):
        HandlerRegistry([InspectHandler(runtime, policy), InspectHandler(runtime, policy)])
    with pytest.raises(ValueError, match="no executable handler"):
        HandlerRegistry([]).for_state(TaskState.INSPECTING)


def test_recovery_handler_refuses_unsafe_runtime_resume(tmp_path: Path) -> None:
    with pytest.raises(RecoveryHandlerUnavailable, match="side-effect report"):
        RecoveryHandler.execute(_context(tmp_path))
