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
    assert search.phase == ProgressPhase.LOCATING_PAGE
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
