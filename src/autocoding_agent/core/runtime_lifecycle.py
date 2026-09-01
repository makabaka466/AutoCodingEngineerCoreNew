"""Shared Runtime bookkeeping used by development and incident orchestration.

This module deliberately owns only mechanical lifecycle work: creating a run,
recording observable activities, projecting safe progress and closing the run.
Domain decisions remain in the two Engines.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection
from typing import Protocol

from autocoding_agent.core.models import AgentEvent, AgentUsage, EventType, utc_now
from autocoding_agent.core.progress import (
    ProgressProjector,
    ProgressSink,
    ProgressWorkflow,
    emit_progress,
)
from autocoding_agent.core.runtime.models import (
    RunStatus,
    RuntimeActivity,
    RuntimeEventKind,
    RuntimeRunRecord,
)
from autocoding_agent.core.state_machine.models import TaskState


class RuntimeAggregate(Protocol):
    """The small session surface required by Runtime lifecycle bookkeeping."""

    id: str
    task_state: TaskState
    runtime_session_id: str | None
    runs: list[RuntimeRunRecord]
    events: list[AgentEvent]


SessionSaver = Callable[[RuntimeAggregate], None]
ErrorFactory = Callable[[str], Exception]


class RuntimeLifecycle:
    """Record one workflow's Runtime runs without owning its business loop."""

    def __init__(
        self,
        *,
        workflow: ProgressWorkflow,
        owner_id: str,
        save: SessionSaver,
        error_factory: ErrorFactory = ValueError,
        record_test_commands: bool = False,
    ) -> None:
        self.workflow = workflow
        self.owner_id = owner_id
        self.save = save
        self.error_factory = error_factory
        self.record_test_commands = record_test_commands

    def start(
        self,
        session: RuntimeAggregate,
        *,
        mode: str,
        command_id: str | None,
    ) -> RuntimeRunRecord:
        """Create the durable run envelope before the external process starts."""

        run = RuntimeRunRecord(
            task_id=session.id,
            state=session.task_state,
            mode=mode,
            owner_id=self.owner_id,
            owner_pid=os.getpid(),
            runtime_session_id=session.runtime_session_id,
        )
        session.runs.append(run)
        session.events.append(
            AgentEvent(
                type=EventType.RUNTIME_STARTED,
                message=f"Started {self.workflow.value} Runtime run in {mode} mode.",
                actor="host",
                command_id=command_id,
                correlation_id=run.id,
                data={
                    "run_id": run.id,
                    "state": run.state.value,
                    "mode": run.mode,
                    "workflow": self.workflow.value,
                },
            )
        )
        return run

    def record_activity(
        self,
        session: RuntimeAggregate,
        run: RuntimeRunRecord,
        activity: RuntimeActivity,
        *,
        command_id: str | None,
        mode: str,
        progress_sink: ProgressSink | None,
        attachment_paths: Collection[str] = (),
    ) -> None:
        """Persist one sanitized activity and publish its optional UI progress phase."""

        if activity.run_id != run.id or run.status != RunStatus.STARTED:
            raise self.error_factory("Runtime activity does not belong to the active run.")
        run.heartbeat_at = max(run.heartbeat_at, activity.created_at)
        run.activity_ids.append(activity.id)
        event_type = {
            RuntimeEventKind.TOOL_STARTED: EventType.TOOL_STARTED,
            RuntimeEventKind.TOOL_FINISHED: EventType.TOOL_FINISHED,
        }.get(activity.kind, EventType.RUNTIME_ACTIVITY)
        event = AgentEvent(
            type=event_type,
            message=activity.summary,
            actor="runtime",
            command_id=command_id,
            correlation_id=run.id,
            data={
                "activity_id": activity.id,
                "run_id": run.id,
                "kind": activity.kind.value,
                "tool_name": activity.tool_name,
                "tool_use_id": activity.tool_use_id,
                "workflow": self.workflow.value,
                **activity.data,
            },
            created_at=activity.created_at,
        )
        session.events.append(event)
        if self.record_test_commands:
            self._record_completed_test(session, run, activity, command_id, event.id)
        self.save(session)

        progress = ProgressProjector.from_runtime(
            activity,
            workflow=self.workflow,
            task_id=session.id,
            mode=mode,
            attachment_paths=attachment_paths,
        )
        if progress is not None:
            emit_progress(progress_sink, progress)

    def finish(
        self,
        session: RuntimeAggregate,
        run: RuntimeRunRecord,
        *,
        status: RunStatus,
        reason: str | None,
        command_id: str | None,
        runtime_session_id: str | None = None,
    ) -> None:
        """Move a started run to one immutable terminal result and emit its event."""

        if run.status != RunStatus.STARTED:
            raise self.error_factory(f"Runtime run {run.id} already has a terminal result.")
        now = utc_now()
        run.status = status
        run.heartbeat_at = now
        run.completed_at = now
        run.terminal_reason = " ".join((reason or "").split())[:500] or None
        run.runtime_session_id = runtime_session_id or run.runtime_session_id
        event_type = {
            RunStatus.COMPLETED: EventType.RUNTIME_COMPLETED,
            RunStatus.FAILED: EventType.RUNTIME_FAILED,
            RunStatus.INTERRUPTED: EventType.RUNTIME_INTERRUPTED,
        }[status]
        session.events.append(
            AgentEvent(
                type=event_type,
                message=f"{self.workflow.value.capitalize()} Runtime run {status.value}.",
                actor="host",
                command_id=command_id,
                correlation_id=run.id,
                data={
                    "run_id": run.id,
                    "mode": run.mode,
                    "terminal_reason": run.terminal_reason,
                    "workflow": self.workflow.value,
                },
            )
        )

    @staticmethod
    def _record_completed_test(
        session: RuntimeAggregate,
        run: RuntimeRunRecord,
        activity: RuntimeActivity,
        command_id: str | None,
        causation_id: str,
    ) -> None:
        if not (
            activity.kind == RuntimeEventKind.TOOL_FINISHED
            and (activity.tool_name or "").casefold() == "bash"
            and not bool(activity.data.get("is_error"))
            and is_test_command(str(activity.data.get("command") or ""))
        ):
            return
        session.events.append(
            AgentEvent(
                type=EventType.TEST_EXECUTED,
                message="The Runtime completed a recognized test command.",
                actor="runtime",
                command_id=command_id,
                correlation_id=run.id,
                causation_id=causation_id,
                data={
                    "run_id": run.id,
                    "tool_use_id": activity.tool_use_id,
                    "command": activity.data.get("command"),
                    "succeeded": True,
                    "host_verified": True,
                },
            )
        )


def merge_usage(current: AgentUsage, new: AgentUsage) -> AgentUsage:
    """Add provider usage from another Runtime call to the current cycle total."""

    return AgentUsage(
        input_tokens=current.input_tokens + new.input_tokens,
        output_tokens=current.output_tokens + new.output_tokens,
        cache_read_tokens=current.cache_read_tokens + new.cache_read_tokens,
        cost_usd=(current.cost_usd or 0) + (new.cost_usd or 0),
        duration_ms=(current.duration_ms or 0) + (new.duration_ms or 0),
        turns=(current.turns or 0) + (new.turns or 0),
    )


def is_test_command(command: str) -> bool:
    """Recognize common validation commands for host-verified audit events."""

    normalized = " ".join(command.casefold().split())
    markers = (
        "pytest",
        "python -m unittest",
        "dotnet test",
        "npm test",
        "npm run test",
        "pnpm test",
        "yarn test",
        "cargo test",
        "go test",
        "mvn test",
        "mvnw test",
        "gradle test",
        "gradlew test",
    )
    return any(marker in normalized for marker in markers)
