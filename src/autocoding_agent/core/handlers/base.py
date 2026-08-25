"""State-handler contracts that keep runtime execution out of AgentEngine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from autocoding_agent.core.models import (
    AgentMode,
    ChatMessage,
    RuntimeResult,
    RuntimeTurn,
)
from autocoding_agent.core.policies import ExecutionPolicy
from autocoding_agent.core.runtime.models import RuntimeActivity
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.ports.runtime import AgentRuntime


@dataclass(frozen=True)
class HandlerContext:
    session_id: str
    runtime_session_id: str | None
    workspace: str
    user_message: str
    history: tuple[ChatMessage, ...]
    system_prompt: str
    capability_dir: str | None
    run_id: str | None = None
    runtime_event_sink: Callable[[RuntimeActivity], None] | None = None


@dataclass(frozen=True)
class HandlerResult:
    runtime: RuntimeResult
    turn: RuntimeTurn


class StateHandler:
    """Execute one externally meaningful runtime phase without persisting state."""

    state: TaskState
    mode: AgentMode

    def __init__(self, runtime: AgentRuntime, policy: ExecutionPolicy) -> None:
        self.runtime = runtime
        self.policy = policy

    def execute(self, context: HandlerContext) -> HandlerResult:
        profile = self.policy.profile(self.mode)
        turn = RuntimeTurn(
            session_id=context.session_id,
            runtime_session_id=context.runtime_session_id,
            workspace=context.workspace,
            user_message=context.user_message,
            history=list(context.history),
            mode=self.mode,
            system_prompt=context.system_prompt,
            tools=list(profile.tools),
            allowed_tools=list(profile.allowed_tools),
            permission_mode=profile.permission_mode,
            capability_dir=context.capability_dir,
        )
        observed = getattr(self.runtime, "run_observed", None)
        if (
            callable(observed)
            and context.run_id is not None
            and context.runtime_event_sink is not None
        ):
            result = observed(turn, context.run_id, context.runtime_event_sink)
        else:
            result = self.runtime.run(turn)
        return HandlerResult(runtime=result, turn=turn)
