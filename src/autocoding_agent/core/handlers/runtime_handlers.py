"""Concrete handlers for the three currently executable task states."""

from __future__ import annotations

from autocoding_agent.core.handlers.base import StateHandler
from autocoding_agent.core.models import AgentMode
from autocoding_agent.core.state_machine.models import TaskState


class InspectHandler(StateHandler):
    state = TaskState.INSPECTING
    mode = AgentMode.INSPECT


class ImplementHandler(StateHandler):
    state = TaskState.IMPLEMENTING
    mode = AgentMode.IMPLEMENT


class VerifyHandler(StateHandler):
    state = TaskState.VERIFYING
    mode = AgentMode.VERIFY


class RecoveryHandlerUnavailable(RuntimeError):
    """Recovery execution is deliberately deferred until side effects can be reconciled."""


class RecoveryHandler:
    state = TaskState.RECOVERY_REQUIRED

    @staticmethod
    def execute(_context: object) -> None:
        raise RecoveryHandlerUnavailable(
            "Recovery requires a host-generated side-effect report before runtime resume."
        )
