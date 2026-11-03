from __future__ import annotations

from autocoding_agent.core.progress import (
    ProgressEvent,
    ProgressPhase,
    ProgressProjector,
    ProgressWorkflow,
    emit_progress,
)
from autocoding_agent.core.runtime.models import RuntimeActivity, RuntimeEventKind


def test_runtime_projector_uses_curated_tool_phases_and_ignores_model_text() -> None:
    read = ProgressProjector.from_runtime(
        RuntimeActivity(
            run_id="run-1",
            kind=RuntimeEventKind.TOOL_STARTED,
            summary="raw runtime summary",
            tool_name="Read",
            data={"path": "src/services/orders.py"},
        ),
        workflow=ProgressWorkflow.DEVELOPMENT,
        task_id="task-1",
        mode="inspect",
    )
    search = ProgressProjector.from_runtime(
        RuntimeActivity(
            run_id="run-2",
            kind=RuntimeEventKind.TOOL_STARTED,
            summary="raw runtime summary",
            tool_name="Grep",
            data={"pattern": "Order details"},
        ),
        workflow=ProgressWorkflow.INCIDENT,
        task_id="task-2",
        mode="inspect",
    )
    assistant_text = ProgressProjector.from_runtime(
        RuntimeActivity(
            run_id="run-2",
            kind=RuntimeEventKind.ASSISTANT_MESSAGE,
            summary="private model reasoning must not become UI status",
        ),
        workflow=ProgressWorkflow.INCIDENT,
        task_id="task-2",
        mode="inspect",
    )

    assert read is not None
    assert read.phase == ProgressPhase.INSPECTING_CODE
    assert read.detail == "orders.py"
    assert search is not None
    assert search.phase == ProgressPhase.INSPECTING_CODE
    assert assistant_text is None


def test_progress_event_trims_details_and_sink_failure_is_non_fatal() -> None:
    event = ProgressEvent.for_phase(
        ProgressWorkflow.INCIDENT,
        ProgressPhase.QUERYING_DATABASE,
        task_id="task-3",
        detail="  query   evidence  " + "x" * 200,
    )

    assert event.label == "正在查询业务数据"
    assert event.detail is not None
    assert len(event.detail) == 120

    def broken_sink(_event: ProgressEvent) -> None:
        raise RuntimeError("UI disconnected")

    emit_progress(broken_sink, event)


def test_heartbeat_does_not_replace_last_observed_business_phase() -> None:
    assert ProgressProjector.from_runtime(
        RuntimeActivity(run_id="run-1", kind=RuntimeEventKind.HEARTBEAT, summary="Alive."),
        workflow=ProgressWorkflow.INCIDENT, task_id="task-1", mode="inspect",
    ) is None


def test_search_budget_progress_and_pause_are_specific_and_do_not_claim_entry_confirmed():
    progress = ProgressProjector.from_runtime(
        RuntimeActivity(run_id="run", kind=RuntimeEventKind.TOOL_STARTED, summary="Search",
                        tool_name="Grep", data={"path": "Business/Binding.cs",
                        "search_calls": 18, "search_limit": 30, "review_due": True}),
        workflow=ProgressWorkflow.INCIDENT, task_id="task", mode="inspect",
    )
    assert progress.phase == ProgressPhase.INSPECTING_CODE
    assert "18/30" in progress.detail
    assert "收敛" in progress.detail
    paused = ProgressProjector.from_runtime(
        RuntimeActivity(run_id="run", kind=RuntimeEventKind.POLICY_BLOCKED, summary="Budget",
                        data={"budget_exhausted": True}),
        workflow=ProgressWorkflow.INCIDENT, task_id="task", mode="inspect",
    )
    assert not paused.active
    assert "暂停" in paused.label
