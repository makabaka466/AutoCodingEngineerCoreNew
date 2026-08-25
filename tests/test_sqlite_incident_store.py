from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocoding_agent.adapters.sqlite_incident_store import SQLiteIncidentStore
from autocoding_agent.adapters.sqlite_task_store import ConcurrentSessionUpdate
from autocoding_agent.core.models import AgentUsage, EventType, RuntimeTurn
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.incident.engine import IncidentEngine
from autocoding_agent.incident.models import (
    IncidentDecision,
    IncidentSession,
    IncidentStatus,
    LocatedPage,
)
from autocoding_agent.ports.structured_runtime import StructuredRuntimeResult


class CompleteIncidentRuntime:
    def run_structured(
        self,
        turn: RuntimeTurn,
        response_model: type[IncidentDecision],
    ) -> StructuredRuntimeResult[IncidentDecision]:
        return StructuredRuntimeResult(
            output=IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="Diagnosis complete.",
                page=LocatedPage(
                    name="Orders",
                    route="/orders/:id",
                    source_paths=["src/orders.py"],
                    explanation="The route matches the report.",
                ),
                diagnosis="The persisted state explains the page symptom.",
            ),
            runtime_session_id=turn.session_id,
            usage=AgentUsage(turns=1),
        )


def test_sqlite_incident_store_persists_replayable_event_timeline(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SQLiteIncidentStore(tmp_path / "data", migrate_legacy_json=False)
    engine = IncidentEngine(CompleteIncidentRuntime(), store, None)

    outcome = engine.start(workspace, "Order page is stale", "/orders/42")

    events = store.list_events(outcome.session_id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert any(event.type == EventType.RUNTIME_STARTED for event in events)
    assert any(event.type == EventType.RUNTIME_COMPLETED for event in events)
    assert store.replay_task_state(outcome.session_id) == TaskState.COMPLETED
    assert store.list_runs(outcome.session_id)[0].status.value == "completed"


def test_sqlite_incident_store_rejects_stale_snapshot_save(tmp_path: Path) -> None:
    store = SQLiteIncidentStore(tmp_path / "data", migrate_legacy_json=False)
    session = IncidentSession(workspace=str(tmp_path), problem="Investigate")
    store.create(session)
    first = store.load(session.id)
    stale = store.load(session.id)
    first.page_hint = "Orders"
    store.save(first)
    stale.page_hint = "Inventory"

    with pytest.raises(ConcurrentSessionUpdate):
        store.save(stale)


def test_sqlite_incident_store_imports_legacy_json_without_overwriting_it(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    legacy_dir = data_dir / "incidents"
    legacy_dir.mkdir(parents=True)
    decision = IncidentDecision(
        status=IncidentStatus.COMPLETED,
        message="Legacy diagnosis.",
        page=LocatedPage(
            name="Orders",
            source_paths=["src/orders.py"],
            explanation="Legacy page evidence.",
        ),
        diagnosis="Legacy root cause.",
    )
    legacy = IncidentSession(
        workspace=str(tmp_path),
        problem="Legacy incident",
        status=IncidentStatus.COMPLETED,
        last_decision=decision,
    )
    payload = legacy.model_dump(
        mode="json",
        exclude={"task_state", "version", "revision", "events", "runs", "command_receipts"},
    )
    source = legacy_dir / f"{legacy.id}.json"
    original = json.dumps(payload, ensure_ascii=False, indent=2)
    source.write_text(original, encoding="utf-8")

    store = SQLiteIncidentStore(data_dir)
    restored = store.load(legacy.id)

    assert restored.task_state == TaskState.COMPLETED
    assert store.replay_task_state(legacy.id) == TaskState.COMPLETED
    assert source.read_text(encoding="utf-8") == original
    assert any(event.actor == "migration" for event in store.list_events(legacy.id))
