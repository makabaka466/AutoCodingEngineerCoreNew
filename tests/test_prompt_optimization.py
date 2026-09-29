"""阶段精简只能改变输入说明，不能改变权限、证据门槛或结构化结果。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_claude_runtime import _turn
from test_incident_flow import FakeDatabase, ScriptedStructuredRuntime, _page

from autocoding_agent.adapters.claude_code import ClaudeCodeRuntime
from autocoding_agent.adapters.json_incident_store import JsonIncidentStore
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentDecision, AgentMode
from autocoding_agent.core.workflow import WORKFLOW_RULES
from autocoding_agent.incident.engine import IncidentEngine
from autocoding_agent.incident.models import (
    DataQuery,
    IncidentContinuationDecision,
    IncidentContinuationStatus,
    IncidentDecision,
    IncidentQueryStage,
    IncidentStatus,
)
from autocoding_agent.incident.prompting import load_incident_workflow_rules
from autocoding_agent.skills import SkillRegistry


@pytest.mark.parametrize("mode", list(AgentMode))
def test_phase_methods_keep_common_safety_and_all_applicable_methods(mode: AgentMode):
    full = SkillRegistry(compact_phase_prompts=False)
    optimized = SkillRegistry()
    baseline = full.build_system_prompt(mode, None)
    actual = optimized.build_system_prompt(mode, None)
    assert actual.split('<skill name=')[0] == baseline.split('<skill name=')[0]
    assert WORKFLOW_RULES in actual
    for name, content in full.load_all():
        exclusive = (name == "implement_change" and mode != AgentMode.IMPLEMENT) or (
            name == "verify_change" and mode != AgentMode.VERIFY
        )
        assert (content in actual) == (not exclusive)
    assert len(actual) < len(baseline)


@pytest.mark.parametrize("name", ["custom_method", "implement_change", "verify_change"])
def test_custom_working_methods_are_not_filtered(tmp_path: Path, name: str):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "SKILL.md").write_text("Custom safety invariant.", encoding="utf-8")
    for mode in AgentMode:
        assert "Custom safety invariant." in SkillRegistry(tmp_path).build_system_prompt(mode, None)


def test_incident_diagnosis_keeps_causal_sql_completion_and_global_rules_verbatim():
    full = load_incident_workflow_rules()
    compact = load_incident_workflow_rules("diagnosis")
    head = full.split("## 1. Assess")[0]
    diagnostic_rules = full[full.index("## 4. Trace"):]
    assert head in compact and diagnostic_rules in compact
    for rule in ["matched_evidence", "unresolved_conflicts", "changed, denied", "at most 20 rows"]:
        assert rule in compact
    assert len(compact) < len(full) * 0.75
    assert load_incident_workflow_rules("unknown") == full


@pytest.mark.parametrize("response_model", [AgentDecision, IncidentDecision])
def test_compact_schema_preserves_every_description_and_constraint(tmp_path: Path, response_model):
    runtime = ClaudeCodeRuntime(Settings(data_dir=tmp_path))
    turn = _turn(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text(turn.system_prompt, encoding="utf-8")
    command = runtime.build_command(turn, response_model, system_prompt_file=prompt)
    encoded = command[command.index("--json-schema") + 1]
    schema = response_model.model_json_schema()
    assert json.loads(encoded) == schema
    assert len(encoded) < len(json.dumps(schema, ensure_ascii=False))


@pytest.mark.parametrize("conflict", [False, True])
def test_incident_profiles_have_identical_host_results_and_next_input_restores_full(
    tmp_path: Path, conflict: bool,
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    query = DataQuery(name="order_state", purpose="Check state", sql="SELECT id FROM orders")
    page = _page()
    final_page = page.model_copy(deep=True)
    if conflict:
        final_page.unresolved_conflicts = ["Screenshot title differs from candidate source."]
    decisions = [
        IncidentDecision(
            status=IncidentStatus.QUERY_REQUIRED, message="Check current data", page=page,
            query_stage=IncidentQueryStage.BUSINESS_DATA, queries=[query],
        ),
        IncidentDecision(
            status=IncidentStatus.COMPLETED, message="A qualified candidate cause.",
            page=final_page, diagnosis="The row state supports a hypothesis, not causal proof.",
            recommended_actions=["Check the missing failure log before changing code."],
            confidence=0.6,
        ),
        IncidentContinuationDecision(
            status=IncidentContinuationStatus.INVESTIGATE, reuse_verified_page=True,
            message="New evidence requires inspection",
        ),
        IncidentDecision(
            status=IncidentStatus.NEEDS_INPUT, message="Need current failure evidence",
            question="What time did the new error occur?",
        ),
    ]
    outcomes = []
    for enabled in [False, True]:
        runtime = ScriptedStructuredRuntime([item.model_copy(deep=True) for item in decisions])
        database = FakeDatabase()
        engine = IncidentEngine(
            runtime, JsonIncidentStore(tmp_path / str(enabled)), database,
            compact_phase_prompts=enabled,
        )
        outcome = engine.start(workspace, "Order 42 remains pending", "/orders/42")
        outcomes.append(outcome)
        assert len(runtime.turns) == 2
        assert database.queries == [query]
        assert "## 1. Assess" in runtime.turns[0].system_prompt
        assert ("## Previously checked page identity" in runtime.turns[1].system_prompt) == enabled
        assert runtime.turns[0].tools == ["Read"]
        assert runtime.turns[1].tools == ["Read", "Glob", "Grep"]
        if not conflict:
            engine.send(outcome.session_id, "The same order now has a different error")
            assert "## 1. Assess" in runtime.turns[-1].system_prompt
            assert "## Previously checked page identity" not in runtime.turns[-1].system_prompt
    left, right = outcomes
    for key in [
        "status", "task_state", "message", "diagnosis", "page", "recommended_actions", "confidence",
    ]:
        assert getattr(left, key) == getattr(right, key)
    assert (right.status == IncidentStatus.NEEDS_INPUT) == conflict


def test_rollback_can_be_selected_without_editing_code(monkeypatch):
    monkeypatch.setenv("AUTO_CODING_COMPACT_PHASE_PROMPTS", "false")
    assert Settings().compact_phase_prompts is False



def test_failed_business_query_keeps_full_rules_until_successful_evidence(tmp_path: Path):
    class FailOnceDatabase(FakeDatabase):
        def execute(self, query):
            if not self.queries:
                self.queries.append(query)
                raise RuntimeError("Invalid column; retry the corrected query.")
            return super().execute(query)

    query = DataQuery(
        name="order_state", purpose="Check current state", sql="SELECT id FROM orders",
    )
    request = IncidentDecision(
        status=IncidentStatus.QUERY_REQUIRED, message="Check state", page=_page(),
        query_stage=IncidentQueryStage.BUSINESS_DATA, queries=[query],
    )
    runtime = ScriptedStructuredRuntime([
        request, request.model_copy(deep=True),
        IncidentDecision(
            status=IncidentStatus.COMPLETED, message="Qualified cause",
            page=_page(), diagnosis="Current data is not causal proof.",
            recommended_actions=["Check failure logs."],
        ),
    ])
    engine = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "state"), FailOnceDatabase())
    outcome = engine.start(tmp_path, "Order 42 pending", "/orders/42")
    assert outcome.status == IncidentStatus.COMPLETED
    assert len(runtime.turns) == 3
    assert "## 1. Assess" in runtime.turns[1].system_prompt
    assert "## Previously checked page identity" in runtime.turns[2].system_prompt
