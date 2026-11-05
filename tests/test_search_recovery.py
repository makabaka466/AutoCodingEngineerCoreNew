"""搜索额度耗尽后的真实持久化与恢复边界，覆盖两个业务流程。"""

import json

import pytest

from autocoding_agent.adapters.claude_code import ClaudeCodeSearchPolicyError
from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentDecision, AgentUsage, RuntimeResult
from autocoding_agent.core.runtime.models import RuntimeActivity, RuntimeEventKind
from autocoding_agent.core.search_policy import SearchPolicyViolation
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.ports.structured_runtime import StructuredRuntimeResult


class BudgetRuntime:
    def __init__(self, block_kind="budget"):
        self.turns = []
        self.block_kind = block_kind

    def run_observed(self, turn, run_id, event_sink):
        output = self.run_structured_observed(turn, AgentDecision, run_id, event_sink)
        return RuntimeResult(decision=output.output, runtime_session_id=turn.session_id)

    def run_structured_observed(self, turn, response_model, run_id, event_sink):
        self.turns.append(turn)
        if len(self.turns) == 1:
            for kind in (RuntimeEventKind.TOOL_STARTED, RuntimeEventKind.TOOL_FINISHED):
                event_sink(RuntimeActivity(
                    run_id=run_id, kind=kind, summary="Read target",
                    tool_name="Read", tool_use_id="read-target",
                    data={"path": "Page.cs", "is_error": False},
                ))
            event_sink(RuntimeActivity(
                run_id=run_id, kind=RuntimeEventKind.ASSISTANT_MESSAGE,
                summary="目标 Page.cs；待核对 SaveBinding 的重复校验。",
            ))
            event_sink(RuntimeActivity(
                run_id=run_id, kind=RuntimeEventKind.TOOL_STARTED,
                summary="Search dependency", tool_name="Grep", tool_use_id="search-save",
                data={"path": "Page.cs", "pattern": "SaveBinding", "head_limit": 20,
                      "search_calls": 30, "search_limit": 30, "directory_calls": {".": 30}},
            ))
        if self.block_kind != "budget" and len(self.turns) <= (
            2 if self.block_kind == "retryable" else 1
        ):
            raise ClaudeCodeSearchPolicyError(SearchPolicyViolation(
                tool_name="Grep", reason="目录级 Grep 必须提供 glob 或 type 文件过滤器",
                retryable=self.block_kind == "retryable",
            ))
        if self.block_kind == "budget" and len(self.turns) == 1:
            raise ClaudeCodeSearchPolicyError(SearchPolicyViolation(
                tool_name="Grep", reason="单轮 Glob/Grep 调用超过 30 次预算",
                budget_exhausted=True,
            ))
        return StructuredRuntimeResult(
            output=response_model(status="needs_input", message="已恢复调查，需确认业务规则。",
                                  question="同一码是否允许跨工单绑定？"),
            runtime_session_id=turn.session_id,
            usage=AgentUsage(),
        )


@pytest.mark.parametrize("workflow", ["development", "incident"])
@pytest.mark.parametrize("changed", [False, True])
def test_budget_pause_survives_restart_and_resumes_with_evidence(tmp_path, workflow, changed):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = workspace / "Page.cs"
    source.write_text("void SaveBinding() {}", encoding="utf-8")
    runtime = BudgetRuntime()
    settings = Settings(data_dir=tmp_path / "state", claude_command="unused.exe")
    build = build_application if workflow == "development" else build_incident_application
    app = build(settings=settings, runtime=runtime)
    paused = app.start(workspace, "Page.cs：调整绑定校验")
    assert paused.task_state.value == "paused"
    assert "无需重复提供页面路径" in paused.message
    assert len(runtime.turns) == 1  # 不自动重试以绕过总预算。
    saved = app.get_session(paused.session_id)
    assert saved.runs[-1].status.value == "interrupted"
    assert saved.investigation_entry is None  # Read 不能伪造入口身份确认。
    assert not any(e.type.value == "task_failed" for e in saved.events)
    if changed:
        source.write_text("void SaveBinding() { Validate(); }", encoding="utf-8")
    app = build(settings=settings, runtime=runtime)
    resumed = app.resume(paused.session_id)
    assert resumed.task_state.value == "waiting_input"
    assert len(runtime.turns) == 2
    assert runtime.turns[-1].mode.value == "inspect"
    context = runtime.turns[-1].user_message
    assert '"path": "Page.cs"' in context
    assert '"unchanged": ' + json.dumps(not changed) in context
    assert "SaveBinding" in context
    assert "待核对" in context
    assert '"search_calls": 30' in context
    assert app.get_session(paused.session_id).runs[0].status.value == "interrupted"


@pytest.mark.parametrize("workflow", ["development", "incident"])
@pytest.mark.parametrize("block_kind", ["retryable", "nonretryable"])
@pytest.mark.parametrize("changed", [False, True])
def test_search_block_pauses_and_preserves_reads_after_restart(
    tmp_path, workflow, block_kind, changed,
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = workspace / "Page.cs"
    source.write_text("void SaveBinding() {}", encoding="utf-8")
    runtime = BudgetRuntime(block_kind)
    settings = Settings(data_dir=tmp_path / "state", claude_command="unused.exe")
    build = build_application if workflow == "development" else build_incident_application
    app = build(settings=settings, runtime=runtime)
    paused = app.start(workspace, f"{source}：调整绑定校验")
    calls = 2 if block_kind == "retryable" else 1
    assert len(runtime.turns) == calls
    assert paused.task_state.value == "paused"
    assert "内部源码搜索受阻" in paused.message
    assert "目录级 Grep 必须提供 glob 或 type 文件过滤器" in paused.message
    assert "无需为此重复提供路径或补充截图" in paused.message
    saved = app.get_session(paused.session_id)
    assert saved.investigation_entry is None  # 不能用成功读取冒充入口身份确认。
    assert saved.runs[-1].status.value == "failed"
    assert not any(e.type.value == "task_failed" for e in saved.events)
    if changed:
        source.write_text("void SaveBinding() { Validate(); }", encoding="utf-8")
    app = build(settings=settings, runtime=runtime)
    resumed = app.resume(paused.session_id)
    assert len(runtime.turns) == calls + 1
    assert resumed.task_state.value == "waiting_input"  # 真实业务疑问仍可正常澄清。
    assert runtime.turns[-1].mode.value == "inspect"
    context = runtime.turns[-1].user_message
    assert '"path": "Page.cs"' in context
    assert '"unchanged": ' + json.dumps(not changed) in context
    assert "SaveBinding" in context
    assert "待核对" in context


def test_budget_after_approved_edit_requires_recovery_and_preserves_changes(tmp_path):
    from test_recovery import CrashAfterEditRuntime

    class BudgetAfterEdit(CrashAfterEditRuntime):
        def run(self, turn):
            try:
                return super().run(turn)
            except RuntimeError:
                raise ClaudeCodeSearchPolicyError(SearchPolicyViolation(
                    tool_name="Grep", reason="单轮 Glob/Grep 调用超过 30 次预算",
                    budget_exhausted=True,
                )) from None

    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = workspace / "service.py"
    source.write_text("value = 1\n", encoding="utf-8")
    runtime = BudgetAfterEdit()
    app = build_application(settings=Settings(data_dir=tmp_path / "state"), runtime=runtime)
    pending = app.start(workspace, "Update service.py")
    interrupted = app.approve(pending.session_id)
    assert interrupted.task_state.value == "recovery_required"
    assert runtime.calls == 2
    assert source.read_text(encoding="utf-8") == "value = 2\n"
    session = app.get_session(pending.session_id)
    assert any(item.type.value == "changes_patch" for item in session.artifacts)
    assert session.runs[-1].status.value == "interrupted"
