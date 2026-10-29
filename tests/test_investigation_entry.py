"""入口身份与执行边界的回归；不调用真实模型或业务库。"""

import pytest
from test_agent_flows import ScriptedRuntime
from test_incident_flow import ScriptedStructuredRuntime

from autocoding_agent.adapters.capability_store import CapabilityStore
from autocoding_agent.adapters.json_incident_store import JsonIncidentStore
from autocoding_agent.adapters.sqlite_incident_store import SQLiteIncidentStore
from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.investigation import InvestigationEntry, accept_entry
from autocoding_agent.core.models import AgentEvent, EventType
from autocoding_agent.core.runtime.models import RuntimeActivity, RuntimeEventKind
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.core.workflow import WorkflowEvidence, read_fingerprint
from autocoding_agent.database_models import DataQuery, QueryObservation
from autocoding_agent.incident.engine import IncidentEngine
from autocoding_agent.incident.models import IncidentDecision, IncidentSession, IncidentStatus
from autocoding_agent.workspace_knowledge import KnowledgeDomain, MarkdownKnowledgeService


def entry(kind="api", **updates):
    values = dict(
        kind=kind,
        state="confirmed",
        label="POST /orders",
        source_paths=["handler.py"],
        reason="用户明确的 POST /orders 与路由注册及处理方法一致。",
        evidence=[WorkflowEvidence(kind="code", reference="handler.py")],
    )
    values.update(updates)
    return InvestigationEntry(**values)


def record_read(session, workspace):
    (workspace / "handler.py").write_text("route = '/orders'", encoding="utf-8")
    session.events.append(
        AgentEvent(
            type=EventType.TOOL_FINISHED,
            message="Read handler",
            data={
                "tool_name": "Read",
                "path": "handler.py",
                "is_error": False,
                "content_sha256": read_fingerprint(str(workspace), "handler.py"),
            },
        )
    )


@pytest.mark.parametrize("kind", ["page", "api", "background"])
def test_confirmed_code_entry_requires_actual_current_read(tmp_path, kind):
    session = IncidentSession(workspace=str(tmp_path), problem="异常")
    (tmp_path / "handler.py").write_text("route = '/orders'", encoding="utf-8")
    forged = entry(kind)
    forged.evidence[0].verified = True
    assert accept_entry(session, forged).state == "candidate"
    record_read(session, tmp_path)
    assert accept_entry(session, entry(kind)).state == "confirmed"
    (tmp_path / "handler.py").write_text("route = '/other'", encoding="utf-8")
    assert accept_entry(session, entry(kind)).state == "candidate"


@pytest.mark.parametrize("path", ["../outside.py", "C:/Windows/system.ini", "missing.py"])
def test_entry_rejects_outside_or_missing_source(tmp_path, path):
    session = IncidentSession(workspace=str(tmp_path), problem="异常")
    record_read(session, tmp_path)
    checked = accept_entry(session, entry(source_paths=[path]))
    assert checked.state == "candidate"
    assert checked.conflicts


def test_excluded_entry_is_persisted_and_cannot_be_silently_revived(tmp_path):
    session = IncidentSession(workspace=str(tmp_path), problem="异常")
    record_read(session, tmp_path)
    accept_entry(session, entry())
    accept_entry(session, entry(state="excluded", reason="用户确认此接口不属于目标系统。"))
    store = JsonIncidentStore(tmp_path / "state")
    store.create(session)
    restored = store.load(session.id)
    assert restored.investigation_entry.state == "excluded"
    assert restored.entry_history[0].state == "confirmed"
    assert accept_entry(restored, entry()).state == "candidate"
    assert restored.investigation_entry.conflicts


def test_data_identity_requires_query_from_the_declared_source(tmp_path):
    session = IncidentSession(workspace=str(tmp_path), problem="订单状态异常")
    session.events.append(
        AgentEvent(
            type=EventType.DATABASE_QUERIES_EXECUTED,
            message="Query",
            data={
                "queries": [
                    {"query_name": "orders", "status": "succeeded", "datasource_id": "test"}
                ]
            },
        )
    )
    session.query_observations = [
        QueryObservation(query_name="orders", datasource_id="test", purpose="确认订单对象")
    ]
    candidate = entry(
        "data",
        source_paths=[],
        data_source="production",
        data_object="ORDERS",
        evidence=[WorkflowEvidence(kind="query", reference="orders")],
    )
    assert accept_entry(session, candidate).state == "candidate"
    candidate.data_source = "test"
    assert accept_entry(session, candidate).state == "confirmed"


def test_data_can_collect_evidence_without_page_but_cannot_query_another_source():
    candidate = entry(
        "data",
        state="candidate",
        source_paths=[],
        data_source="test",
        data_object="ORDERS",
        evidence=[],
    )
    decision = IncidentDecision(
        status="query_required",
        message="确认对象",
        entry=candidate,
        query_stage="business_data",
        queries=[
            DataQuery(
                name="orders",
                datasource_id="test",
                purpose="确认对象",
                sql="SELECT id FROM ORDERS",
                max_rows=1,
            )
        ],
    )
    allowed, deferred = IncidentEngine._defer_unresolved_page_conflict(decision)
    assert not deferred and allowed.page is None
    decision.queries[0].datasource_id = "production"
    blocked, deferred = IncidentEngine._defer_unresolved_page_conflict(decision)
    assert deferred and blocked.status == IncidentStatus.NEEDS_INPUT


class ObservedEntryRuntime(ScriptedStructuredRuntime):
    investigation_entry_version = 1

    def run_structured_observed(self, turn, response_model, run_id, activity_sink):
        activity_sink(
            RuntimeActivity(
                run_id=run_id,
                kind=RuntimeEventKind.TOOL_FINISHED,
                summary="Read API handler",
                tool_name="Read",
                data={"path": "handler.py", "is_error": False},
            )
        )
        return self.run_structured(turn, response_model)


@pytest.mark.parametrize("kind", ["api", "background"])
def test_non_page_investigation_completes_and_survives_reload(tmp_path, kind):
    (tmp_path / "handler.py").write_text("route = '/orders'", encoding="utf-8")
    runtime = ObservedEntryRuntime(
        [
            IncidentDecision(
                status="completed",
                entry=entry(kind),
                message="已核对入口，原因尚待现场日志验证。",
                diagnosis="处理分支可能未覆盖空状态。",
                recommended_actions=["核对现场日志。"],
            )
        ]
    )
    engine = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "state"), None)
    outcome = engine.start(tmp_path, "POST /orders 返回异常，请分析接口。")
    assert outcome.status == IncidentStatus.COMPLETED
    assert outcome.page is None
    assert outcome.entry.state == "confirmed"
    assert "调查入口" in outcome.message
    assert engine.get_session(outcome.session_id).investigation_entry == outcome.entry
    assert runtime.turns[0].tools == ["Read", "Glob", "Grep"]
    assert len(runtime.turns) == 1  # 不新增独立的入口分类模型调用。


def test_forged_completion_is_deferred_without_querying_or_claiming_success(tmp_path):
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status="completed",
                entry=entry(),
                message="完成",
                diagnosis="猜测原因",
            )
        ]
    )
    engine = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "state"), None)
    outcome = engine.start(tmp_path, "POST /orders 异常")
    assert outcome.status == IncidentStatus.NEEDS_INPUT
    assert not engine.get_session(outcome.session_id).can_remediate


def test_navigation_is_shared_but_other_projects_are_not_indexed(tmp_path):
    service = MarkdownKnowledgeService(tmp_path / "knowledge-state")
    service.ensure_project("one")
    service.ensure_project("two")
    service.save_branch(KnowledgeDomain.NAVIGATION, "one", "# One navigation\nCurrent routes")
    service.save_branch(KnowledgeDomain.NAVIGATION, "two", "# Other project secrets")
    for domain in ("development", "incident"):
        store = CapabilityStore(tmp_path / domain, service.knowledge_root / domain)
        directory = store.prepare(tmp_path, "one")
        index = (directory / "CAPABILITIES.md").read_text(encoding="utf-8")
        assert "navigation.md" in index
        assert "Other project" not in index
        assert (
            (directory / "pinned/one/navigation.md")
            .read_text(encoding="utf-8")
            .rstrip()
            .endswith("Current routes")
        )
    assert service.load_branch(KnowledgeDomain.DEVELOPMENT, "one")[1] == ""


def test_remediation_source_revision_is_checked_before_approval(tmp_path):
    settings = Settings(data_dir=tmp_path / "state")
    app = build_application(settings, runtime=ScriptedRuntime())
    incident = IncidentSession(
        workspace=str(tmp_path),
        problem="异常",
        status="completed",
        task_state=TaskState.COMPLETED,
        investigation_entry=entry(),
        last_decision=IncidentDecision(
            status="completed",
            entry=entry(),
            message="诊断",
            diagnosis="待验证",
            recommended_actions=["修复处理分支"],
        ),
    )
    store = SQLiteIncidentStore(settings.data_dir)
    store.create(incident)
    from autocoding_agent.core.models import AgentSession

    task = AgentSession(
        workspace=str(tmp_path),
        goal="处理异常",
        source_incident_key=f"{incident.id}:1",
        source_incident_revision=incident.revision,
    )
    app._engine.sessions.create(task)
    assert app._engine._remediation_source_current(task)
    incident.cycle_number += 1
    store.save(incident)
    assert not app._engine._remediation_source_current(task)
    with pytest.raises(ValueError, match="原审批失效"):
        app.approve(task.id)
    assert app.get_session(task.id).pending_approval is None


def test_entry_contract_cannot_be_bypassed_by_omitting_entry(tmp_path):
    from test_incident_flow import _page

    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status="completed",
                page=_page(),
                message="完成",
                diagnosis="无操作证据的旧页面",
            )
        ]
    )
    runtime.investigation_entry_version = 1
    engine = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "state"), None)
    result = engine.start(tmp_path, "调查异常")
    assert result.status == IncidentStatus.NEEDS_INPUT
    assert result.entry.kind == "unknown"


def test_reconsider_excluded_requires_explicit_correction_and_fresh_evidence(tmp_path):
    session = IncidentSession(workspace=str(tmp_path), problem="异常")
    record_read(session, tmp_path)
    accept_entry(session, entry(state="excluded", reason="用户否认所属系统"))
    changed_name = entry(label="Alias /orders")
    assert accept_entry(session, changed_name).state == "candidate"
    changed_name.reconsider_excluded = True
    assert accept_entry(session, changed_name).state == "candidate"
    record_read(session, tmp_path)
    changed_name.reason = "用户纠正系统说明，新读取的路由注册证明原排除依据不成立。"
    assert accept_entry(session, changed_name).state == "confirmed"

    changed_name.reconsider_excluded = False
    assert accept_entry(session, changed_name).state == "confirmed"


def test_changed_diagnosis_during_runtime_pauses_before_next_operation(tmp_path):
    from test_agent_flows import _app

    from autocoding_agent.core.models import AgentDecision

    runtime = ScriptedRuntime(AgentDecision(status="needs_input", message="等待现场信息"))
    app = _app(tmp_path / "state", runtime)
    app._engine.source_incident_current = lambda session: not runtime.turns
    result = app._engine.start(
        tmp_path, "按诊断修复", source_incident_key="source:1", source_incident_revision=1
    )
    saved = app.get_session(result.session_id)
    assert saved.task_state == TaskState.PAUSED
    assert saved.pending_approval is None
    assert len(runtime.turns) == 1
    assert saved.runs[-1].status.value == "completed"
    assert any("原方案及审批已失效" in item.content for item in saved.messages)


def test_remediation_handoff_carries_candidate_and_requires_fresh_inspection(tmp_path):
    from test_agent_flows import _app, _approval, _diagnosed_incident

    from autocoding_agent.core.models import ApprovalScope

    runtime = ScriptedRuntime(_approval(ApprovalScope.MODIFY))
    app = _app(tmp_path / "state", runtime)
    app._engine.source_incident_current = lambda session: True
    incident = _diagnosed_incident(tmp_path)
    incident.investigation_entry = entry()
    result = app.start_incident_remediation(incident)
    assert result.status.value == "needs_input"
    saved = app.get_session(result.session_id)
    assert saved.investigation_entry.state == "candidate"
    assert saved.pending_approval is None
    assert "POST /orders" in runtime.turns[0].user_message


def test_legacy_page_conflict_cannot_be_hidden_by_confirmed_entry(tmp_path):
    from test_incident_flow import _page

    (tmp_path / "handler.py").write_text("route = '/orders'", encoding="utf-8")
    page = _page().model_copy(
        update={"source_paths": ["handler.py"], "unresolved_conflicts": ["截图标题与候选冲突"]}
    )
    runtime = ObservedEntryRuntime(
        [
            IncidentDecision(
                status="completed",
                entry=entry("page"),
                page=page,
                message="完成",
                diagnosis="尚未验证的原因",
            )
        ]
    )
    result = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "state"), None).start(
        tmp_path,
        "核对异常页面",
    )
    assert result.status == IncidentStatus.NEEDS_INPUT
    assert result.entry.conflicts == page.unresolved_conflicts


def test_missing_entry_does_not_implicitly_reuse_old_identity(tmp_path):
    (tmp_path / "handler.py").write_text("route = '/orders'", encoding="utf-8")
    runtime = ObservedEntryRuntime(
        [
            IncidentDecision(
                status="needs_input", entry=entry(), message="入口已核对", question="异常何时发生？"
            ),
            IncidentDecision(status="completed", message="完成", diagnosis="旧入口原因"),
        ]
    )
    engine = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "state"), None)
    first = engine.start(tmp_path, "调查接口")
    result = engine.send(first.session_id, "那不是我的接口")
    assert result.status == IncidentStatus.NEEDS_INPUT
    assert result.entry.state == "candidate"
    assert not engine.get_session(result.session_id).can_remediate
