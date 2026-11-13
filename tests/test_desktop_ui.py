from __future__ import annotations

import gc
import tkinter as tk
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from test_datasources import catalog as catalog

from autocoding_agent.config import Settings
from autocoding_agent.core.approval_execution import snapshot_approval
from autocoding_agent.core.models import (
    AgentDecision,
    AgentEvent,
    AgentOutcome,
    AgentSession,
    AgentStatus,
    ApprovalRequest,
    ApprovalScope,
    ChangeProposal,
    ChatMessage,
    EventType,
    MessageAttachment,
    MessageRole,
    ProposedChange,
)
from autocoding_agent.core.progress import (
    ProgressEvent,
    ProgressPhase,
    ProgressSink,
    ProgressWorkflow,
)
from autocoding_agent.core.recovery.models import RecoveryAction
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.core.workflow import ResultKind, TaskAnalysis, WorkflowAssessment
from autocoding_agent.database_models import QueryObservation, QueryObservationStatus
from autocoding_agent.embedding_setup import (
    EmbeddingConnectionConfig,
    EmbeddingSetupState,
)
from autocoding_agent.incident.models import (
    IncidentCompletionKind,
    IncidentDecision,
    IncidentFinding,
    IncidentOutcome,
    IncidentSession,
    IncidentStatus,
)
from autocoding_agent.interfaces.chat_transcript import ChatTranscript
from autocoding_agent.interfaces.desktop_ui import (
    COLORS,
    DesktopClient,
    FlowKind,
    RoundedButton,
    _format_query_observation,
    format_approval_details,
    session_list_label,
)
from autocoding_agent.interfaces.knowledge_management_ui import KnowledgeManagementDialog
from autocoding_agent.interfaces.system_settings_ui import SystemSettingsDialog
from autocoding_agent.knowledge_rag.service import build_fake_rag_service
from autocoding_agent.model_setup import ClaudeInstallation, ModelSetupState
from autocoding_agent.sqlserver_config import (
    SQLServerAuthentication,
    SQLServerConfigState,
    SQLServerConnectionConfig,
)
from autocoding_agent.workspace_config import (
    WorkspaceConfigService,
    WorkspaceConfigState,
    WorkspaceConfigStore,
    WorkspaceProject,
)
from autocoding_agent.workspace_knowledge import KnowledgeDomain, MarkdownKnowledgeService


def test_settings_edits_named_source_and_project_binding(root, catalog, tmp_path):
    from tkinter import ttk
    from types import SimpleNamespace

    service, _, _ = catalog
    knowledge = MarkdownKnowledgeService(tmp_path / "knowledge-state")
    projects = WorkspaceConfigService(
        store=WorkspaceConfigStore(service.settings.data_dir), knowledge=knowledge,
    )
    project = projects.save_project("项目 A", tmp_path)
    dialog = SystemSettingsDialog(
        root,
        SimpleNamespace(inspect=lambda: ModelSetupState(
            installation=ClaudeInstallation(found=False), endpoint="", model="",
            has_api_key=False, ready=False,
        )),
        SimpleNamespace(
            inspect=lambda: SQLServerConfigState(),
            drivers=lambda: ["ODBC Driver 17 for SQL Server"],
            settings=service.settings,
        ),
        workspace_service=projects, knowledge_service=knowledge,
    )
    dialog.notebook.select(dialog.database_tab)
    dialog.source_combo.current(2)
    dialog._select_source()
    assert dialog.db_name_var.get() == "ds-menu"
    assert dialog.db_password_var.get() == ""
    dialog.db_name_var.set("edited-menu")
    dialog._save_database()
    editor = next(
        child for child in dialog.window.winfo_children() if isinstance(child, tk.Toplevel)
    )
    next(child for child in editor.winfo_children() if isinstance(child, ttk.Button)).invoke()
    assert service.connection("ds-menu").inspect().config.database == "edited-menu"
    assert service.connection("ds-business").inspect().config.database == "ds-business"
    assert not (service.settings.data_dir / "database" / "sqlserver.json").exists()
    dialog._edit_project_sources()
    editor = next(
        child for child in dialog.window.winfo_children() if isinstance(child, tk.Toplevel)
    )
    choices = next(child for child in editor.winfo_children() if isinstance(child, tk.Listbox))
    choices.selection_set(0)
    choices.selection_set(1)
    next(child for child in editor.winfo_children() if isinstance(child, ttk.Button)).invoke()
    assert service.load().projects[project.id].source_ids == ["ds-menu", "ds-business"]
    root.deiconify()
    dialog.window.deiconify()
    dialog.window.update()
    assert dialog.database_tab.winfo_reqheight() <= dialog.database_tab.winfo_height()


def test_oracle_dialog_saves_independent_source_and_does_not_echo_password(root, catalog):
    from autocoding_agent.interfaces.oracle_settings_ui import OracleSettingsDialog

    service, _, _ = catalog
    dialog = OracleSettingsDialog(root, service)
    for key, value in {
        "name": "车载 Oracle", "environment": "test", "schema_name": "MES",
        "server": "oracle.example", "database": "MES_SERVICE", "username": "reader",
        "password": "oracle-secret",
    }.items():
        dialog.variables[key].set(value)
    root.deiconify()
    dialog.window.update()
    assert dialog.variables["mode"].get() == "thin"
    assert dialog.window.winfo_reqheight() < dialog.window.winfo_screenheight()
    dialog._save()
    definition = service.load().sources[dialog.source_id]
    assert definition.database_type == "oracle"
    assert service.connection(dialog.source_id).inspect().configured
    editor = OracleSettingsDialog(root, service, definition)
    assert editor.variables["database"].get() == "MES_SERVICE"
    assert editor.variables["password"].get() == ""
    editor.variables["database"].set("MES_OTHER")
    editor._save()
    assert service.connection(dialog.source_id).inspect().config.database == "MES_OTHER"
    assert service.connection("ds-menu").inspect().config.database == "ds-menu"


class FakeApplication:
    def __init__(self, sessions: list[AgentSession] | None = None) -> None:
        self.sessions = {item.id: item for item in sessions or []}
        self.calls: list[tuple[str, ...]] = []
        self.approval_versions: list[int | None] = []

    def list_sessions(self) -> list[AgentSession]:
        return list(self.sessions.values())

    def get_session(self, session_id: str) -> AgentSession:
        return self.sessions[session_id]

    def start(
        self,
        workspace: str | Path,
        message: str,
        project: str | None = None,
        *,
        progress_sink: ProgressSink | None = None,
    ) -> AgentOutcome:
        self.calls.append(("start", str(workspace), message, project or ""))
        session = AgentSession(
            id=str(uuid4()),
            workspace=str(workspace),
            goal=message,
            project=project,
            status=AgentStatus.NEEDS_INPUT,
        )
        self.sessions[session.id] = session
        return AgentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=AgentStatus.NEEDS_INPUT,
            message="请补充信息",
        )

    def send(
        self,
        session_id: str,
        message: str,
        *,
        progress_sink: ProgressSink | None = None,
    ) -> AgentOutcome:
        self.calls.append(("send", session_id, message))
        session = self.sessions[session_id]
        return AgentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=session.status or AgentStatus.NEEDS_INPUT,
            message="继续处理",
        )

    def approve(
        self,
        session_id: str,
        *,
        expected_version: int | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> AgentOutcome:
        self.calls.append(("approve", session_id))
        self.approval_versions.append(expected_version)
        session = self.sessions[session_id]
        return AgentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=AgentStatus.COMPLETED,
            message="已完成",
        )

    def reject(
        self,
        session_id: str,
        reason: str = "",
        *,
        progress_sink: ProgressSink | None = None,
    ) -> AgentOutcome:
        self.calls.append(("reject", session_id, reason))
        session = self.sessions[session_id]
        return AgentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=AgentStatus.NEEDS_INPUT,
            message="已拒绝",
        )

    def resume(
        self,
        session_id: str,
        action: RecoveryAction = RecoveryAction.READ_ONLY_INSPECT,
        *,
        progress_sink: ProgressSink | None = None,
    ) -> AgentOutcome:
        self.calls.append(("resume", session_id, action.value))
        session = self.sessions[session_id]
        return AgentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=AgentStatus.NEEDS_INPUT,
            message="已恢复",
        )

    def cancel(
        self,
        session_id: str,
        *,
        progress_sink: ProgressSink | None = None,
    ) -> AgentOutcome:
        self.calls.append(("cancel", session_id))
        session = self.sessions[session_id]
        return AgentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=AgentStatus.FAILED,
            message="已取消",
        )


class FakeIncidentApplication:
    def __init__(self, sessions: list[IncidentSession] | None = None) -> None:
        self.sessions = {item.id: item for item in sessions or []}
        self.calls: list[tuple[str, ...]] = []
        self.attachments: list[list[MessageAttachment]] = []

    def list_sessions(self) -> list[IncidentSession]:
        return list(self.sessions.values())

    def get_session(self, session_id: str) -> IncidentSession:
        return self.sessions[session_id]

    def start(
        self,
        workspace: str | Path,
        problem: str,
        page_hint: str | None = None,
        *,
        project: str | None = None,
        attachments: list[MessageAttachment] | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> IncidentOutcome:
        self.calls.append(("start", str(workspace), problem, page_hint or "", project or ""))
        self.attachments.append(list(attachments or []))
        session = IncidentSession(
            id=str(uuid4()),
            workspace=str(workspace),
            problem=problem,
            project=project,
            page_hint=page_hint,
            status=IncidentStatus.NEEDS_INPUT,
        )
        self.sessions[session.id] = session
        return IncidentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=IncidentStatus.NEEDS_INPUT,
            message="请补充异常信息",
            question="受影响的记录是什么？",
        )

    def send(
        self,
        session_id: str,
        message: str,
        command_id: str | None = None,
        attachments: list[MessageAttachment] | None = None,
        *,
        progress_sink: ProgressSink | None = None,
    ) -> IncidentOutcome:
        self.calls.append(("send", session_id, message))
        self.attachments.append(list(attachments or []))
        session = self.sessions[session_id]
        return IncidentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=IncidentStatus.NEEDS_INPUT,
            message="继续诊断",
        )

    def resume(
        self,
        session_id: str,
        action: RecoveryAction = RecoveryAction.READ_ONLY_INSPECT,
        *,
        progress_sink: ProgressSink | None = None,
    ) -> IncidentOutcome:
        self.calls.append(("resume", session_id, action.value))
        session = self.sessions[session_id]
        return IncidentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=IncidentStatus.NEEDS_INPUT,
            task_state=TaskState.WAITING_INPUT,
            message="已恢复异常诊断",
            question="请补充记录编号",
        )

    def cancel(
        self,
        session_id: str,
        *,
        progress_sink: ProgressSink | None = None,
    ) -> IncidentOutcome:
        self.calls.append(("cancel", session_id))
        session = self.sessions[session_id]
        return IncidentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=IncidentStatus.FAILED,
            task_state=TaskState.CANCELLED,
            message="已取消异常诊断",
        )


class FakeWorkspaceService:
    def __init__(self, path: Path, **_kwargs) -> None:
        self.project = WorkspaceProject(id="ace-test", name="生物", path=str(path))
        self.state = WorkspaceConfigState(config=self.project, available=path.is_dir())

    def inspect(self) -> WorkspaceConfigState:
        return self.state

    def list_projects(self) -> list[WorkspaceProject]:
        self.knowledge.ensure_project(self.project.id, legacy_name=self.project.name)
        return [self.project]


@pytest.fixture(autouse=True)
def isolated_project_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # UI tests must never migrate or modify the developer's actual project catalog/knowledge.
    for module in ("desktop_ui", "system_settings_ui"):
        monkeypatch.setattr(
            f"autocoding_agent.interfaces.{module}.WorkspaceConfigService",
            lambda **kwargs: FakeWorkspaceService(Path.cwd()),
        )
        monkeypatch.setattr(
            f"autocoding_agent.interfaces.{module}.MarkdownKnowledgeService",
            lambda: MarkdownKnowledgeService(tmp_path / "knowledge-state"),
        )


@pytest.fixture(scope="session")
def tk_root() -> Iterator[tk.Tk]:
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk is unavailable: {exc}")
    window.withdraw()
    yield window
    if window.winfo_exists():
        window.destroy()


@pytest.fixture
def root(tk_root: tk.Tk) -> Iterator[tk.Toplevel]:
    window = tk.Toplevel(tk_root)
    window.withdraw()
    yield window
    if window.winfo_exists():
        window.destroy()
    # Tk 对象必须在主线程回收，避免后续 HTTP worker 的 GC 触发 Tcl 调用。
    gc.collect()


def _session(status: AgentStatus = AgentStatus.NEEDS_INPUT) -> AgentSession:
    return AgentSession(
        id=str(uuid4()),
        workspace=str(Path.cwd()),
        goal="修复 src/order.py 中的重复扣库存问题",
        status=status,
        messages=[
            ChatMessage(role=MessageRole.USER, content="请检查 order.py"),
            ChatMessage(role=MessageRole.ASSISTANT, content="我已经读取目标文件。"),
        ],
        updated_at=datetime(2026, 8, 20, 2, 30, tzinfo=timezone.utc),
    )


def _capture_operation(
    client: DesktopClient,
) -> list[Callable[[], AgentOutcome | IncidentOutcome]]:
    operations: list[Callable[[], AgentOutcome | IncidentOutcome]] = []

    def capture(
        operation: Callable[[], AgentOutcome | IncidentOutcome],
        _label: str,
    ) -> None:
        operations.append(operation)

    client._run_in_background = capture  # type: ignore[method-assign]
    return operations


def test_analysis_history_is_under_each_turn_and_restores_without_duplicates(root, tmp_path):
    session = _session()
    start = session.messages[0].created_at
    session.messages[1].created_at = start + timedelta(seconds=3)
    session.messages.append(ChatMessage(role=MessageRole.USER, content="补充限额规则",
                                       created_at=start + timedelta(seconds=4)))
    session.events = [
        AgentEvent(type=EventType.TURN_STARTED, message="开始调查",
                   created_at=start + timedelta(seconds=1)),
        AgentEvent(type=EventType.RUNTIME_ACTIVITY, message="先确认入口与计数规则",
                   data={"kind": "assistant_message"}, created_at=start + timedelta(seconds=2)),
        AgentEvent(type=EventType.TURN_STARTED, message="继续任务",
                   created_at=start + timedelta(seconds=5)),
        AgentEvent(type=EventType.STATE_TRANSITIONED, message="停止",
                   data={"to": "paused", "reason": "用户停止，状态已保存"},
                   created_at=start + timedelta(seconds=6)),
    ]
    client = DesktopClient(root, FakeApplication([session]),
                           workspace_service=FakeWorkspaceService(tmp_path))
    client.session_id = session.id
    client._render_session(session)
    assert [role for role, _ in client._chat_renderer.entries] == [
        "user", "analysis", "assistant", "user", "analysis",
    ]
    assert client.transcript.get("1.0", "end").count("展开分析过程") == 2
    original = list(client._chat_renderer.entries)
    first_key = original[1][1]
    client._chat_renderer.toggle("analysis:" + first_key)
    assert "收起分析过程" in client.transcript.get("1.0", "end")
    client._render_session(session)
    assert client._chat_renderer.entries == original
    assert "analysis:" + first_key in client._chat_renderer.expanded
    client._render_welcome()
    assert not client._chat_renderer.analysis_sections
    client._render_session(session)
    assert client._chat_renderer.entries == original


def test_analysis_groups_reads_and_lazily_shows_older_details(root):
    from autocoding_agent.interfaces.analysis_trace import AnalysisTrace

    trace = AnalysisTrace()
    trace.append([AgentEvent(type=EventType.TOOL_FINISHED, message="返回",
                            data={"tool_name": "Read", "tool_use_id": str(i),
                                  "path": f"src/file_{i}.py", "is_error": i == 65})
                  for i in range(70)])
    widget = tk.Text(root)
    renderer = ChatTranscript(widget)
    renderer.analysis_sections = trace.sections
    renderer.render([("analysis", "legacy")])
    assert "src/file_69.py" in widget.get("1.0", "end")
    assert "src/file_1.py" not in widget.get("1.0", "end")
    renderer.toggle("analysis:legacy")
    assert "读取文件 · 50 次" in widget.get("1.0", "end")
    assert widget.tag_ranges("chat_warning")
    renderer.toggle("analysis-step:" + trace.sections["legacy"].steps[0].key)
    assert "src/file_20.py" in widget.get("1.0", "end")
    assert "src/file_1.py" not in widget.get("1.0", "end")
    trace.append([AgentEvent(type=EventType.TOOL_FINISHED, message="返回",
                            data={"tool_name": "Read", "tool_use_id": "70",
                                  "path": "src/file_70.py", "is_error": False})])
    renderer.render(renderer.entries, preserve_scroll=True)
    assert "src/file_21.py" in widget.get("1.0", "end")
    assert "analysis-step:" + trace.sections["legacy"].steps[0].key in renderer.expanded
    renderer._more_analysis("legacy")
    assert "src/file_1.py" in widget.get("1.0", "end")
    renderer.collapse_all()
    assert "src/file_1.py" not in widget.get("1.0", "end")


def test_analysis_poll_is_background_bounded_and_does_not_end_task(root, tmp_path):
    import threading
    import time

    session = _session()
    app = FakeApplication([session])
    entered, release = threading.Event(), threading.Event()
    calls = []
    def read_events(task_id, *, after_sequence, limit):
        calls.append((task_id, after_sequence, limit, threading.get_ident()))
        entered.set()
        assert release.wait(timeout=2)
        return [AgentEvent(type=EventType.RUNTIME_ACTIVITY, sequence=1,
                           message="检查绑定事件后再看数据库", data={"kind": "assistant_message"})]
    app.events = read_events
    client = DesktopClient(root, app, workspace_service=FakeWorkspaceService(tmp_path))
    client.session_id = session.id
    client._render_session(session)
    client._task_operation, client._active_task_id = True, session.id
    client._set_busy(True, "调查中")
    try:
        client._poll_analysis()
        assert entered.wait(timeout=1)
        client._poll_analysis()
        assert len(calls) == 1 and calls[0][:3] == (session.id, 0, 200)
        assert calls[0][3] != threading.get_ident()
        root.update_idletasks()  # 数据库尚未返回时，主线程仍可布局。
        release.set()
        deadline = time.monotonic() + 2
        while client._analysis_trace.cursor != 1 and time.monotonic() < deadline:
            client._drain_results()
            time.sleep(.01)
        assert client._analysis_trace.cursor == 1
        assert client._busy and client._task_operation
        assert "检查绑定事件后再看数据库" in client.transcript.get("1.0", "end")
        client._poll_analysis()
        assert len(calls) == 1  # 无新事件时也不高频全量拉取。
    finally:
        release.set()
        client._set_busy(False)


def test_analysis_drops_stale_and_foreign_window_results_and_preserves_selection(root, tmp_path):
    sessions = [_session(), _session()]
    app = FakeApplication(sessions)
    client = DesktopClient(root, app, workspace_service=FakeWorkspaceService(tmp_path))
    client.session_id = sessions[0].id
    client._render_session(sessions[0])
    child = client._open_task_window(session=sessions[1])
    client._task_operation, client._active_task_id = True, sessions[0].id
    client._set_busy(True, "调查中")
    item = AgentEvent(type=EventType.RUNTIME_ACTIVITY, sequence=1,
                      message="当前任务的公开说明", data={"kind": "assistant_message"})
    generation, identity = client._analysis_generation, client._analysis_identity
    for wrong_generation, wrong_identity in ((generation - 1, identity),
                                             (generation, child._analysis_identity)):
        client._result_queue.put(("analysis_events", (wrong_generation, wrong_identity, [item])))
    client._drain_results()
    assert client._analysis_trace.cursor == 0
    client.transcript.tag_add("sel", "1.0", "1.2")
    client._result_queue.put(("analysis_events", (generation, identity, [item])))
    client._drain_results()
    assert client.transcript.tag_ranges("sel") and client._analysis_dirty
    assert "当前任务的公开说明" not in child.transcript.get("1.0", "end")
    client.transcript.tag_remove("sel", "1.0", "end")
    client._poll_analysis()
    assert client._analysis_dirty  # 隐藏页面只投影，切回时才刷新。
    client._show_conversation()
    assert "当前任务的公开说明" in client.transcript.get("1.0", "end")
    client._set_busy(False)
    client._result_queue.put(("analysis_events", (generation, identity, [
        item.model_copy(update={"id": "late", "sequence": 2}),
    ])))
    client._drain_results()
    assert client._analysis_trace.cursor == 1
    child._on_close()


def test_session_list_label_is_compact_and_includes_status() -> None:
    session = _session(AgentStatus.COMPLETED)

    label = session_list_label(session, max_length=12)

    assert "…" in label
    assert "已完成" in label
    assert "08-20" in label


def test_chat_long_response_folds_losslessly_and_copies_original(root: tk.Toplevel) -> None:
    widget = tk.Text(root)
    renderer = ChatTranscript(widget)
    content = "## 结论\n\n已确认原因。\n\n" + "详细证据。" * 400 + "\n最终约束不可遗漏。"
    renderer.render([("assistant", content), ("metadata", "内部查询记录\n只读查询已完成")])
    visible = widget.get("1.0", "end")
    assert "结论" in visible and "展开全文" in visible
    assert "最终约束不可遗漏" not in visible and "内部查询记录" not in visible
    renderer.toggle(renderer.key(0, "assistant", content))
    assert "最终约束不可遗漏" in widget.get("1.0", "end")
    renderer._copy(content)
    assert widget.clipboard_get() == content
    renderer.collapse_all()
    assert "最终约束不可遗漏" not in widget.get("1.0", "end")


def test_chat_markdown_styles_do_not_parse_code_or_user_input(root: tk.Toplevel) -> None:
    widget = tk.Text(root)
    renderer = ChatTranscript(widget)
    renderer.render([
        ("user", "**这是原始输入**"),
        ("assistant", "## 修改结果\n- **已修复** `app.py`\n```python\n# literal\n```"),
    ])
    visible = widget.get("1.0", "end")
    assert "**这是原始输入**" in visible
    assert "## 修改结果" not in visible and "修改结果" in visible
    assert "# literal" in visible and "```" not in visible
    assert widget.tag_ranges("chat_heading")
    assert widget.tag_ranges("chat_bold") and widget.tag_ranges("chat_inline_code")
    assert widget.tag_ranges("chat_code")


def test_chat_expansion_survives_refresh_without_changing_message_data(root: tk.Toplevel) -> None:
    widget = tk.Text(root)
    renderer = ChatTranscript(widget)
    entries = [("assistant", "结论\n" + "证据" * 900)]
    renderer.render(entries)
    renderer.expand_all()
    renderer.render(entries)
    assert entries[0][1] in widget.get("1.0", "end")
    assert entries[0][1].endswith("证据" * 900)


def test_query_observation_display_distinguishes_stage_and_failure() -> None:
    success = QueryObservation(
        query_name="resolve_menu",
        purpose="Locate page.",
        stage="page_lookup",
        returned_rows=5,
        truncated=True,
    )
    failure = QueryObservation(
        query_name="load_upload_rows",
        purpose="Inspect business data.",
        status=QueryObservationStatus.FAILED,
        stage="business_data",
        error="Read-only query failed: invalid column",
    )

    assert _format_query_observation(success) == "• [页面定位] resolve_menu: 5 行，已截断"
    assert _format_query_observation(failure) == (
        "• [业务数据] load_upload_rows: 失败 · Read-only query failed: invalid column"
    )


def test_approval_details_show_before_after_plan_and_preview() -> None:
    approval = ApprovalRequest(
        scope=ApprovalScope.MODIFY,
        reason="The UI needs a clearer approval flow.",
        proposal=ChangeProposal(
            summary="Show a reviewable plan before any file is edited.",
            changes=[
                ProposedChange(
                    path="src/ui.py",
                    area="approval card",
                    current="Only a one-line reason is shown.",
                    proposed="Show current state, target state, preview, impact, and validation.",
                )
            ],
            expected_result="The user can review the exact change before approval.",
            impact=["No repository edit happens during this review turn."],
            validation=["Render the approval card with a fake application."],
            preview_markdown="[方案] -> [用户确认] -> [实施]",
        ),
    )

    detail = format_approval_details(approval)

    for expected in (
        "Show a reviewable plan",
        "### 1. approval card\n\n文件：`src/ui.py`",
        "**修改前**\n\nOnly a one-line reason is shown.",
        "**修改后**\n\nShow current state",
        "The user can review the exact change",
        "No repository edit",
        "Render the approval card",
        "[方案] -> [用户确认] -> [实施]",
    ):
        assert expected in detail


def test_approval_details_explain_when_a_preview_is_not_reliable() -> None:
    approval = ApprovalRequest(
        scope=ApprovalScope.MODIFY,
        reason="Change internal cleanup behavior.",
        proposal=ChangeProposal(
            summary="Adjust an internal cleanup boundary.",
            changes=[
                ProposedChange(
                    area="cleanup",
                    current="Temporary data remains.",
                    proposed="Temporary data is removed after success.",
                )
            ],
            expected_result="No stale temporary data remains.",
        ),
    )

    detail = format_approval_details(approval)

    assert "不适合在实施前生成可信预览" in detail
    assert "None" not in detail


def test_legacy_modify_approval_can_be_revised_but_not_approved(
    root: tk.Toplevel,
) -> None:
    session = _session(AgentStatus.APPROVAL_REQUIRED)
    session.pending_approval = ApprovalRequest(
        scope=ApprovalScope.MODIFY,
        reason="旧版修改请求",
        proposed_actions=["编辑 src/order.py"],
    )
    client = DesktopClient(root, FakeApplication([session]))  # type: ignore[arg-type]
    client.session_id = session.id

    client._render_session(session)

    assert client.approve_button.cget("state") == "normal"
    assert client.approve_button.cget("text") == "重新核对"
    assert client.reject_button.cget("state") == "normal"
    assert "缺少修改方案" in client.approval_issue.cget("text")
    operations = _capture_operation(client)
    client._approve()
    assert not operations
    client.approve_button.invoke()
    operations.pop()()
    assert client.application.calls[0][:2] == ("send", session.id)
    assert "只读重新核对" in client.application.calls[0][2]


def test_approval_snapshot_shows_scope_gap_and_requires_revision(root: tk.Toplevel) -> None:
    session = _session(AgentStatus.APPROVAL_REQUIRED)
    session.pending_approval = ApprovalRequest(
        scope=ApprovalScope.MODIFY,
        reason="修复库存逻辑",
        proposal=ChangeProposal(
            summary="修复重复扣减",
            changes=[ProposedChange(
                area="库存扣减", current="取消后库存不正确", proposed="取消时恢复库存",
            )],
            expected_result="库存准确",
        ),
    )
    session.pending_approval_snapshot = snapshot_approval(session, session.pending_approval)
    client = DesktopClient(root, FakeApplication([session]))  # type: ignore[arg-type]
    client.session_id = session.id

    client._render_session(session)

    displayed = client.approval_text.get("1.0", "end")
    assert session.pending_approval_snapshot.id[:8] in displayed
    assert "缺少明确的工作区内文件路径" in displayed
    assert client.approve_button.cget("state") == "normal"
    assert client.approve_button.cget("command") == client._recheck_approval
    assert "缺少明确" in client.approval_issue.cget("text")
    assert client.reject_button.cget("state") == "normal"


def test_recovery_state_shows_explicit_safe_choices(root: tk.Toplevel) -> None:
    session = _session(AgentStatus.FAILED)
    session.task_state = TaskState.RECOVERY_REQUIRED
    application = FakeApplication([session])
    client = DesktopClient(root, application)  # type: ignore[arg-type]
    client.session_id = session.id
    operations = _capture_operation(client)

    client._render_session(session)

    assert "恢复任务" in client.approval_title.cget("text")
    assert client.approve_button.cget("text") == "继续任务（先核对）"
    assert client.reject_button.cget("text") == "取消任务"
    assert client.recovery_replan_button.winfo_manager() == "grid"
    client._replan_recovery()
    operations[0]()
    assert application.calls == [("resume", session.id, "replan")]


def test_incident_recovery_uses_same_explicit_recovery_controls(root: tk.Toplevel) -> None:
    session = IncidentSession(
        workspace=str(Path.cwd()),
        problem="订单页面诊断中断",
        status=IncidentStatus.FAILED,
        task_state=TaskState.PAUSED,
    )
    application = FakeIncidentApplication([session])
    client = DesktopClient(
        root,
        FakeApplication(),  # type: ignore[arg-type]
        application,  # type: ignore[arg-type]
    )
    client._flow_session_ids[FlowKind.INCIDENT] = session.id
    operations = _capture_operation(client)

    client._select_flow(FlowKind.INCIDENT)

    assert "恢复异常诊断" in client.approval_title.cget("text")
    assert client.approve_button.cget("text") == "继续只读诊断"
    assert client.reject_button.cget("text") == "取消诊断"
    assert client.recovery_replan_button.cget("text") == "重新调查"
    client._resume_recovery()
    operations[0]()
    assert application.calls == [("resume", session.id, "read_only_inspect")]


def test_new_task_routes_first_message_to_application_start(
    root: tk.Toplevel, tmp_path: Path
) -> None:
    application = FakeApplication()
    client = DesktopClient(
        root,
        application,  # type: ignore[arg-type]
        workspace_service=FakeWorkspaceService(tmp_path),  # type: ignore[arg-type]
    )
    operations = _capture_operation(client)
    client.prompt_input.insert("1.0", "调查 src/app.py 的报错")

    client._send_message()
    outcome = operations[0]()

    assert application.calls == [("start", str(tmp_path), "调查 src/app.py 的报错", "ace-test")]
    assert outcome.status == AgentStatus.NEEDS_INPUT


def test_existing_task_routes_message_to_same_session(root: tk.Toplevel) -> None:
    session = _session()
    application = FakeApplication([session])
    client = DesktopClient(root, application)  # type: ignore[arg-type]
    client.session_id = session.id
    client._render_session(session)
    operations = _capture_operation(client)
    client.prompt_input.insert("1.0", "入口函数是 cancel_order")

    client._send_message()
    operations[0]()

    assert application.calls == [("send", session.id, "入口函数是 cancel_order")]


def test_completed_task_keeps_composer_available_for_follow_up(root: tk.Toplevel) -> None:
    session = _session(AgentStatus.COMPLETED)
    application = FakeApplication([session])
    client = DesktopClient(root, application)  # type: ignore[arg-type]
    client.session_id = session.id
    operations = _capture_operation(client)

    client._render_session(session)
    assert client.prompt_input.cget("state") == "normal"
    assert not hasattr(client, "workspace_entry")
    assert client.send_button.cget("text") == "继续对话"
    assert "继续追问" in client.prompt_placeholder.cget("text")
    client.prompt_input.insert("1.0", "继续说明重试边界")
    client._send_message()
    operations[0]()
    assert application.calls == [("send", session.id, "继续说明重试边界")]

    client._new_task()
    fresh = client._window_group[-1]
    assert fresh is not client and client.session_id == session.id
    assert fresh.prompt_input.cget("state") == "normal"
    assert fresh.send_button.cget("text") == "发送任务"
    assert "任务目标" in fresh.prompt_placeholder.cget("text")


def test_completed_incident_keeps_composer_available_for_follow_up(
    root: tk.Toplevel,
) -> None:
    session = IncidentSession(
        workspace=str(Path.cwd()),
        problem="订单页面状态异常",
        status=IncidentStatus.COMPLETED,
        task_state=TaskState.COMPLETED,
    )
    incident_application = FakeIncidentApplication([session])
    client = DesktopClient(
        root,
        FakeApplication(),  # type: ignore[arg-type]
        incident_application,  # type: ignore[arg-type]
    )
    client._flow_session_ids[FlowKind.INCIDENT] = session.id
    client._select_flow(FlowKind.INCIDENT)
    operations = _capture_operation(client)

    assert client.prompt_input.cget("state") == "normal"
    assert client.send_button.cget("text") == "继续对话"
    assert "异常线索" in client.prompt_placeholder.cget("text")
    client.prompt_input.insert("1.0", "继续确认刷新按钮的查询")
    client._send_message()
    operations[0]()

    assert incident_application.calls == [
        ("send", session.id, "继续确认刷新按钮的查询")
    ]


def test_busy_state_blocks_conflicting_controls(root: tk.Toplevel) -> None:
    client = DesktopClient(root, FakeApplication())  # type: ignore[arg-type]

    client._set_busy(True, "处理中")

    assert client.send_button.cget("state") == "disabled"
    assert client.new_task_button.cget("state") == "normal"
    assert client.sessions_list.cget("state") == "normal"
    assert client.model_config_button.cget("state") == "disabled"
    assert client.knowledge_database_button.cget("state") == "disabled"


def test_knowledge_management_lists_markdown_without_auto_indexing(
    root: tk.Toplevel,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "product"
    document = project_root / "knowledge" / "development" / "生物" / "生物.md"
    document.parent.mkdir(parents=True)
    document.write_text("# 生物项目\n\n## 页面定位\n\n使用 Menu.URL 定位代码。\n", encoding="utf-8")
    settings = Settings(
        claude_command="claude-test.exe",
        claude_model="test-model",
        claude_timeout_seconds=30,
        data_dir=tmp_path / "state",
        max_budget_usd=None,
    )
    service = build_fake_rag_service(settings, project_root=project_root)
    dialog = KnowledgeManagementDialog(root, service)

    try:
        assert dialog.service.simulated is True
        assert dialog.service.model_id == "fake-hash-embedding-v1"
        assert len(dialog.documents) == 1
        indexed = next(iter(dialog.documents.values()))
        assert indexed.status.value == "pending"
        assert indexed.chunk_count == 0
        assert dialog.tree.item(indexed.id, "values")[0] == "待加入"
        assert "1 份待加入或重建" in dialog.status_var.get()
    finally:
        dialog.close()


def test_system_settings_combines_model_and_shared_database_without_revealing_secrets(
    root: tk.Toplevel,
    tmp_path: Path,
) -> None:
    model_state = ModelSetupState(
        installation=ClaudeInstallation(
            found=True,
            command="D:/claude/claude.exe",
            version="2.1.228",
        ),
        endpoint="https://provider.example/anthropic",
        model="model-v4",
        has_api_key=True,
        ready=True,
    )

    database_state = SQLServerConfigState(
        config=SQLServerConnectionConfig(
            server="sql.internal",
            database="orders",
            driver="ODBC Driver 17 for SQL Server",
            authentication=SQLServerAuthentication.SQL_PASSWORD,
            username="shared_reader",
        ),
        has_password=True,
    )
    embedding_state = EmbeddingSetupState(
        config=EmbeddingConnectionConfig(
            endpoint="https://api.voyageai.com/v1/embeddings",
            model="voyage-code-4",
            output_dimension=1024,
        ),
        has_api_key=True,
    )

    class FakeModelService:
        def inspect(self, _command: str | None = None) -> ModelSetupState:
            return model_state

    class FakeDatabaseService:
        settings = Settings(data_dir=tmp_path / "state")
        def inspect(self) -> SQLServerConfigState:
            return database_state

        def drivers(self) -> list[str]:
            return ["ODBC Driver 17 for SQL Server"]

    class FakeEmbeddingService:
        def __init__(self) -> None:
            self.saved: list[tuple[EmbeddingConnectionConfig, str]] = []

        def inspect(self) -> EmbeddingSetupState:
            return embedding_state

        def defaults(self) -> EmbeddingConnectionConfig:
            return EmbeddingConnectionConfig()

        def build_config(
            self,
            *,
            endpoint: str,
            model: str,
            output_dimension: int | str,
        ) -> EmbeddingConnectionConfig:
            return EmbeddingConnectionConfig(
                endpoint=endpoint,
                model=model,
                output_dimension=int(output_dimension),
            )

        def save(
            self,
            config: EmbeddingConnectionConfig,
            api_key: str = "",
        ) -> EmbeddingSetupState:
            self.saved.append((config, api_key))
            return embedding_state

    embedding_service = FakeEmbeddingService()

    knowledge_service = MarkdownKnowledgeService(tmp_path / "state")
    knowledge_service.create_branch(KnowledgeDomain.DEVELOPMENT, "生物")
    workspace_service = FakeWorkspaceService(tmp_path)
    dialog = SystemSettingsDialog(
        root,
        FakeModelService(),  # type: ignore[arg-type]
        FakeDatabaseService(),  # type: ignore[arg-type]
        embedding_service=embedding_service,  # type: ignore[arg-type]
        knowledge_service=knowledge_service,
        workspace_service=workspace_service,  # type: ignore[arg-type]
    )

    assert [dialog.notebook.tab(tab, "text") for tab in dialog.notebook.tabs()] == [
        "模型与 Claude Code",
        "Embedding",
        "数据库",
        "项目维护",
        "MD 能力配置",
    ]
    root.update_idletasks()
    assert dialog.model_save_button.winfo_manager() == "pack"
    assert dialog.model_key_var.get() == ""
    assert dialog.model_key_entry.cget("show") != ""
    assert "留空保持不变" in dialog.model_key_hint_var.get()
    assert dialog.embedding_endpoint_var.get() == "https://api.voyageai.com/v1/embeddings"
    assert dialog.embedding_model_var.get() == "voyage-code-4"
    assert dialog.embedding_dimension_var.get() == "1024"
    assert dialog.embedding_key_var.get() == ""
    assert dialog.embedding_key_entry.cget("show") != ""
    assert "留空保持不变" in dialog.embedding_key_hint_var.get()
    assert dialog.db_server_var.get() == "sql.internal"
    assert dialog.db_name_var.get() == "orders"
    assert dialog.db_password_var.get() == ""
    assert dialog.db_password_entry.cget("show") != ""
    assert "留空保持不变" in dialog.database_password_hint_var.get()
    assert "sqlserver://sql.internal:1433/orders" in dialog.database_status_var.get()
    dialog.notebook.select(dialog.database_tab)
    dialog._sync_footer_actions()
    root.update_idletasks()
    assert dialog.database_test_button.winfo_manager() == "pack"
    assert dialog.database_save_button.winfo_manager() == "pack"
    assert dialog.model_save_button.winfo_manager() == ""
    dialog.notebook.select(dialog.embedding_tab)
    dialog._sync_footer_actions()
    root.update_idletasks()
    assert dialog.embedding_test_button.winfo_manager() == "pack"
    assert dialog.embedding_save_button.winfo_manager() == "pack"
    dialog.embedding_key_var.set("new-voyage-key")
    dialog._save_embedding()
    assert embedding_service.saved[0][0].model == "voyage-code-4"
    assert embedding_service.saved[0][1] == "new-voyage-key"
    assert dialog.embedding_key_var.get() == ""
    dialog.notebook.select(dialog.workspace_tab)
    dialog._sync_footer_actions()
    root.update_idletasks()
    assert dialog.workspace_save_button.winfo_manager() == "pack"
    assert dialog.workspace_path_var.get() == str(tmp_path)
    dialog.notebook.select(dialog.knowledge_tab)
    dialog._sync_footer_actions()
    root.update_idletasks()
    assert dialog.knowledge_save_button.winfo_manager() == "pack"
    assert not hasattr(dialog, "knowledge_workspace_entry")
    assert dialog.knowledge_branch_var.get() == "生物"
    assert dialog.knowledge_path_var.get() == "knowledge/development/ace-test/ace-test.md"
    dialog.knowledge_editor.delete("1.0", "end")
    dialog.knowledge_editor.insert("1.0", "# Updated guide\n")
    assert dialog._save_knowledge() is True
    assert "Updated guide" in knowledge_service.branch_path(
        KnowledgeDomain.DEVELOPMENT, "ace-test"
    ).read_text(encoding="utf-8")
    dialog.knowledge_domain_var.set("异常处理")
    dialog._refresh_knowledge()
    assert not hasattr(dialog, "knowledge_add_branch_button")
    assert dialog.knowledge_branch_var.get() == "生物"
    assert dialog.knowledge_path_var.get() == "knowledge/incident/ace-test/ace-test.md"
    dialog._close()


def test_light_theme_and_configured_workspace_keep_composer_compact(
    root: tk.Toplevel,
) -> None:
    client = DesktopClient(root, FakeApplication())  # type: ignore[arg-type]

    assert COLORS["window"] == "#FFFFFF"
    assert not hasattr(client, "workspace_entry")
    assert not hasattr(client, "browse_button")
    assert isinstance(client.send_button, RoundedButton)
    assert client.send_button.cget("background") == COLORS["accent"]
    assert client.send_button.cget("foreground") == "#FFFFFF"
    assert client.transcript.tag_cget("user_message", "background") == COLORS["user"]
    assert client.transcript.cget("cursor") == "xterm"
    assert client.transcript.cget("exportselection") == 0
    assert client.transcript.cget("selectbackground") == COLORS["progress_accent"]
    assert client.transcript.bind("<Button-1>")
    assert client.transcript.bind("<Control-c>")
    assert client.transcript.bind("<Button-3>")
    client._select_all_transcript()
    assert client.transcript.tag_ranges("sel")
    tags = list(client.transcript.tag_names())
    assert tags.index("sel") > tags.index("assistant_message")
    assert tags.index("sel") > tags.index("user_message")
    assert client.status_badge.cget("highlightthickness") == 1
    assert COLORS["progress_accent"] == "#64748B"
    assert client.activity_frame.cget("background") == COLORS["progress_accent_soft"]


def test_status_below_conversation_rotates_only_while_executing(root: tk.Toplevel) -> None:
    client = DesktopClient(root, FakeApplication())  # type: ignore[arg-type]
    assert int(client.activity_frame.grid_info()["row"]) > int(client.transcript.grid_info()["row"])
    client._set_busy(True, "处理中")
    client._animate_progress_pulse()
    angle = client.activity_dot.itemcget(client.activity_spinner, "start")
    client._animate_progress_pulse()
    assert client.activity_dot.itemcget(client.activity_spinner, "start") != angle
    assert client.activity_dot.itemcget(client.activity_spinner, "state") == "normal"

    client._present_progress(ProgressEvent.for_phase(
        ProgressWorkflow.DEVELOPMENT, ProgressPhase.WAITING_INPUT, active=False,
    ))
    client._animate_progress_pulse()
    assert client.activity_dot.itemcget(client.activity_spinner, "state") == "hidden"
    client._set_busy(False)


def test_workspace_layout_search_and_collapsed_project_access(root: tk.Toplevel) -> None:
    first, second = _session(), _session()
    second.goal = "查询物料批次"
    app = FakeApplication([first, second])
    client = DesktopClient(root, app)  # type: ignore[arg-type]
    root.geometry("1440x900")
    root.deiconify()
    root.update()
    assert int(client.main_frame.grid_info()["column"]) == 2
    assert client.sidebar_panel.winfo_width() == 320
    assert not client.overview_panel.winfo_manager()
    client.history_search_var.set("物料")
    assert client._session_ids == [second.id]
    client.sessions_list.selection_set(0)
    client._select_session(None)
    assert client.session_id == second.id
    client.history_search_var.set("")
    assert set(client._session_ids) == {first.id, second.id}
    root.geometry("980x720")
    root.update()
    assert not client.sidebar_panel.winfo_manager()
    client._toggle_history()
    root.update()
    assert client.sidebar_panel.winfo_manager() == "grid"
    assert client.project_combo.winfo_width() > 100


def test_composer_grows_with_text_and_swaps_send_for_stop(root: tk.Toplevel) -> None:
    client = DesktopClient(root, FakeApplication())  # type: ignore[arg-type]
    client.prompt_input.insert("1.0", "需求描述\n" * 20)
    root.update()
    assert 2 < int(client.prompt_input.cget("height")) <= 7
    client.prompt_input.delete("1.0", "end")
    root.update()
    assert int(client.prompt_input.cget("height")) == 2
    client._task_operation = True
    client._set_busy(True, "正在分析")
    assert client.send_button.winfo_manager() == ""
    assert client.stop_button.winfo_manager() == "grid"
    client._set_busy(False)
    assert client.send_button.winfo_manager() == "grid"
    assert client.stop_button.winfo_manager() == ""


def test_review_uses_available_height_and_can_switch_to_conversation(root: tk.Toplevel) -> None:
    session = _session(AgentStatus.APPROVAL_REQUIRED)
    session.pending_approval = ApprovalRequest(
        scope=ApprovalScope.VERIFY, reason="验证修改", proposed_actions=["pytest"],
    )
    session.pending_approval_snapshot = snapshot_approval(session, session.pending_approval)
    client = DesktopClient(root, FakeApplication([session]))  # type: ignore[arg-type]
    root.geometry("1280x800")
    root.deiconify()
    client.session_id = session.id
    client._render_session(session)
    root.update()
    assert client.approval_frame.winfo_height() > 300
    assert client.transcript_panel.winfo_manager() == ""
    client._toggle_review()
    root.update()
    assert client.transcript_panel.winfo_manager() == "panedwindow"
    assert client.approve_button.cget("command") == client._approve
    root.geometry("1840x980")
    root.update()
    assert client.approval_frame.winfo_manager() == ""  # 缩放不能悄悄展开方案。
    client._toggle_review()
    root.update()
    assert client.composer_panel.winfo_width() > 650
    assert client.approval_frame.winfo_manager() == "panedwindow"
    assert len(client.conversation_panes.panes()) == 2
    assert client.send_button.winfo_rootx() + client.send_button.winfo_width() < (
        root.winfo_rootx() + root.winfo_width()
    )


def test_chat_preserves_reading_position_and_copies_literal_code(root: tk.Toplevel) -> None:
    from autocoding_agent.interfaces.chat_transcript import ChatTranscript

    widget = tk.Text(root, width=70, height=16)
    widget.pack(fill="both", expand=True)
    root.geometry("800x600")
    renderer = ChatTranscript(widget)
    code = 'print("**literal**")\n' + "a = 1\n" * 3
    entries = [("user", "需要保留的用户原文"), ("assistant", f"```python\n{code}```")]
    entries += [("assistant", f"第{i}条内容\n" * 4) for i in range(12)]
    renderer.render(entries)
    root.update()
    assert "需要保留的用户原文" in widget.get("1.0", "end")
    block = next(block for block in renderer.blocks if block.code)
    block.copy.invoke()
    assert root.clipboard_get() == code.rstrip("\n")
    widget.yview_moveto(.25)
    before = widget.yview()[0]
    renderer.render(entries + [("assistant", "新回复")], preserve_scroll=True)
    root.update()
    assert abs(widget.yview()[0] - before) < .05


def test_history_restores_result_and_missing_evidence_without_loading(root: tk.Toplevel) -> None:
    assessment = WorkflowAssessment(
        phase=ProgressPhase.COMPLETED, reason="修改已写入，尚未运行验证。",
        missing=["需要运行相关测试。"], result=ResultKind.MODIFIED_UNVERIFIED,
    )
    session = _session(AgentStatus.COMPLETED)
    session.task_state = TaskState.COMPLETED
    session.last_decision = AgentDecision(
        status=AgentStatus.COMPLETED, message="修改完成。", assessment=assessment,
    )
    client = DesktopClient(root, FakeApplication([session]))  # type: ignore[arg-type]
    client.session_id = session.id
    client._render_session(session)
    assert client.activity_var.get() == "修改完成，尚未验证"
    assert "需要运行相关测试" in client.workflow_detail_var.get()
    assert "调查与验证详情" in client.transcript.get("1.0", "end")
    client._chat_renderer.expand_all()
    assert "推进依据" in client.transcript.get("1.0", "end")
    assert not client._progress_event.active
    assert client.activity_dot.itemcget(client.activity_spinner, "state") == "hidden"


def test_paused_task_shows_saved_analysis_without_last_assessment(root: tk.Toplevel) -> None:
    session = _session(AgentStatus.NEEDS_INPUT)
    session.task_state = TaskState.PAUSED
    session.task_analysis = TaskAnalysis(
        goal="核对扫码事件", constraints=["不修改其他接口"],
        investigation=[{"question": "写入前校验在哪里？", "scope": "Page.cs",
                        "done_when": "确认对应写入链", "status": "active"}],
    )
    session.last_decision = AgentDecision(status=AgentStatus.NEEDS_INPUT, message="已暂停")
    client = DesktopClient(root, FakeApplication([session]))  # type: ignore[arg-type]
    client.session_id = session.id
    client._render_session(session)
    assert "核对扫码事件" in client.workflow_detail_var.get()
    assert "写入前校验在哪里" in client.workflow_detail_var.get()
    assert "收束条件" not in client.transcript.get("1.0", "end")
    client._chat_renderer.expand_all()
    assert "不修改其他接口" in client.transcript.get("1.0", "end")
    assert "收束条件" in client.transcript.get("1.0", "end")
    assert not client._progress_event.active


def test_long_evidence_summary_keeps_compact_window_composer_visible(root: tk.Toplevel) -> None:
    session = _session(AgentStatus.NEEDS_INPUT)
    session.task_state = TaskState.WAITING_INPUT
    session.last_decision = AgentDecision(
        status=AgentStatus.NEEDS_INPUT, message="请补充现场证据。",
        assessment=WorkflowAssessment(
            phase=ProgressPhase.WAITING_INPUT,
            reason="已核对页面入口，但缺少现场日志。" * 30,
            missing=["仍需确认对应异常记录与部署版本。" * 20] * 6,
            result=ResultKind.HYPOTHESIS,
        ),
    )
    client = DesktopClient(root, FakeApplication([session]))  # type: ignore[arg-type]
    client.session_id = session.id
    client._render_session(session)
    root.geometry("980x720")
    root.deiconify()
    root.update()  # 完成窗口 Configure 事件后再测量响应式布局。
    assert client.activity_frame.winfo_height() < 160
    assert client.prompt_input.winfo_rooty() + client.prompt_input.winfo_height() <= (
        root.winfo_rooty() + root.winfo_height()
    )
    assert "…" in client.workflow_detail_var.get()
    assert session.last_decision.assessment.reason not in client.transcript.get("1.0", "end")
    client._chat_renderer.expand_all()
    assert session.last_decision.assessment.reason in client.transcript.get("1.0", "end")


def test_terminal_status_cancels_pending_phase_animation(root: tk.Toplevel) -> None:
    client = DesktopClient(root, FakeApplication())  # type: ignore[arg-type]
    client._set_busy(True, "处理中")
    client._present_progress(ProgressEvent.for_phase(
        ProgressWorkflow.DEVELOPMENT, ProgressPhase.INSPECTING_CODE,
    ))
    assert client._pending_progress_event is not None
    client._present_progress(ProgressEvent.for_phase(
        ProgressWorkflow.DEVELOPMENT, ProgressPhase.COMPLETED, active=False,
    ))
    client._flush_pending_progress()
    assert client._progress_event.phase == ProgressPhase.COMPLETED
    assert client._pending_progress_event is None


def test_progress_queue_keeps_worker_busy_and_uses_curated_copy(root: tk.Toplevel) -> None:
    client = DesktopClient(root, FakeApplication())  # type: ignore[arg-type]
    client._set_busy(True, "处理中")
    client._result_queue.put(
        (
            "progress",
            ProgressEvent.for_phase(
                ProgressWorkflow.DEVELOPMENT,
                ProgressPhase.PREPARING_CONTEXT,
                detail="已读取任务配置",
            ),
        )
    )

    client._drain_results()

    assert client._busy is True
    assert client.activity_var.get() == "正在准备任务上下文"
    assert client.activity_detail_var.get() == "已读取任务配置"


def test_overview_uses_real_sessions_and_hides_at_compact_width(
    root: tk.Toplevel,
) -> None:
    now = datetime.now(timezone.utc)
    completed = _session(AgentStatus.COMPLETED)
    completed.created_at = now
    active = _session(AgentStatus.NEEDS_INPUT)
    active.created_at = now
    failed = _session(AgentStatus.FAILED)
    failed.created_at = now - timedelta(days=1)
    client = DesktopClient(
        root,
        FakeApplication([completed, active, failed]),  # type: ignore[arg-type]
    )

    assert client.overview_today_var.get() == "2"
    assert client.overview_completed_var.get() == "1"
    assert client.overview_active_var.get() == "1"
    assert client.overview_rate_var.get() == "33%"
    assert sum(client._trend_counts) == 3

    event = type("ResizeEvent", (), {"widget": root, "width": 1000})()
    client._update_responsive_layout(event)  # type: ignore[arg-type]
    assert client.overview_panel.winfo_manager() == ""


def test_flow_selector_shows_active_flow_and_reveals_incident_fields(
    root: tk.Toplevel,
) -> None:
    incident_application = FakeIncidentApplication()
    client = DesktopClient(
        root,
        FakeApplication(),  # type: ignore[arg-type]
        incident_application,  # type: ignore[arg-type]
    )

    assert client.flow == FlowKind.DEVELOPMENT
    assert client.development_flow_button.selected is True
    assert client.incident_flow_button.selected is False
    assert not hasattr(client, "incident_context_frame")
    assert client.project_var.get() == "生物"
    assert client.project_path_var.get() == "knowledge/development/ace-test/ace-test.md"

    client._select_flow(FlowKind.INCIDENT)

    assert client.flow == FlowKind.INCIDENT
    assert client.incident_flow_button.selected is True
    assert client.development_flow_button.selected is False
    assert "异常诊断" in client.new_task_button.cget("text")
    assert client.task_title_var.get() == "新异常诊断"
    assert "页面、接口、任务或数据对象" in client.prompt_placeholder.cget("text")
    assert "先理解你的对话内容" in client.transcript.get("1.0", "end")
    assert not hasattr(client, "page_hint_entry")
    assert not hasattr(client, "database_browse_button")
    assert client.project_var.get() == "生物"
    assert client.project_path_var.get() == "knowledge/incident/ace-test/ace-test.md"


def test_project_switch_isolates_history_and_routes_new_tasks(
    root: tk.Toplevel, tmp_path: Path,
) -> None:
    knowledge = MarkdownKnowledgeService(tmp_path / "ace")
    service = WorkspaceConfigService(
        store=WorkspaceConfigStore(tmp_path / "state"), knowledge=knowledge,
    )
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    a = service.save_project("项目 A", first)
    b = service.save_project("项目 B", second)
    sa, sb = _session(), _session()
    sa.workspace, sa.project = str(first), a.id
    sb.workspace, sb.project = str(second), b.id
    app = FakeApplication([sa, sb])
    incident = IncidentSession(
        id=str(uuid4()), workspace=str(first), project=a.id, problem="A 异常",
        status=IncidentStatus.NEEDS_INPUT,
    )
    client = DesktopClient(
        root, app, FakeIncidentApplication([incident]),
        knowledge_service=knowledge, workspace_service=service,
    )
    assert client._session_ids == [sa.id]
    client.session_id = sa.id
    client._render_session(sa)
    client.project_var.set(b.name)
    client._on_project_selected(None)
    second_page = client._window_group[-1]
    assert second_page is not client and client.session_id == sa.id
    assert second_page.session_id is None
    assert second_page._session_ids == [sb.id]
    assert second_page.workspace_var.get() == str(second)
    operations = _capture_operation(second_page)
    second_page.prompt_input.insert("1.0", "处理 B")
    second_page._send_message()
    operations[0]()
    assert app.calls[-1] == ("start", str(second), "处理 B", b.id)
    client._select_flow(FlowKind.INCIDENT)
    incident_page = client._window_group[-1]
    assert client.session_id == sa.id and client.flow == FlowKind.DEVELOPMENT
    assert incident_page._session_ids == [incident.id]
    assert incident_page.project_path_var.get().endswith(f"incident/{a.id}/{a.id}.md")
    incident_page.project_var.set(b.name)
    incident_page._on_project_selected(None)
    assert incident_page._session_ids == []
    assert app.get_session(sa.id).workspace == str(first)


def test_project_settings_creates_both_documents_and_keeps_them_on_rename(
    root: tk.Toplevel, tmp_path: Path,
) -> None:
    knowledge = MarkdownKnowledgeService(tmp_path / "ace")
    service = WorkspaceConfigService(
        store=WorkspaceConfigStore(tmp_path / "state"), knowledge=knowledge,
    )
    from types import SimpleNamespace

    dialog = SystemSettingsDialog(
        root, SimpleNamespace(inspect=lambda: ModelSetupState(
            installation=ClaudeInstallation(found=False), endpoint="", model="",
            has_api_key=False, ready=False,
        )),
        SimpleNamespace(
            inspect=lambda: SQLServerConfigState(), drivers=lambda: [],
            settings=Settings(data_dir=tmp_path / "state"),
        ),
        workspace_service=service, knowledge_service=knowledge,
    )
    dialog._new_workspace_project()
    dialog.workspace_name_var.set("新增项目")
    dialog.workspace_path_var.set(str(tmp_path))
    dialog._save_workspace()
    project = service.list_projects()[0]
    assert dialog.knowledge_branch_combo.cget("values") == ("新增项目",)
    assert dialog.knowledge_editor.get("1.0", "end-1c") == ""
    dialog.knowledge_editor.insert("1.0", "业务规则")
    assert dialog._save_knowledge()
    dialog.workspace_name_var.set("改名项目")
    dialog._save_workspace()
    assert service.list_projects()[0].id == project.id
    assert dialog.knowledge_branch_var.get() == "改名项目"
    assert dialog.knowledge_editor.get("1.0", "end-1c").strip() == "业务规则"
    dialog.knowledge_domain_var.set("异常处理")
    dialog._refresh_knowledge()
    assert dialog.knowledge_editor.get("1.0", "end-1c") == ""
    dialog._close()


def test_incident_flow_routes_problem_from_configured_workspace(
    root: tk.Toplevel,
    tmp_path: Path,
) -> None:
    incident_application = FakeIncidentApplication()
    client = DesktopClient(
        root,
        FakeApplication(),  # type: ignore[arg-type]
        incident_application,  # type: ignore[arg-type]
        workspace_service=FakeWorkspaceService(tmp_path),  # type: ignore[arg-type]
    )
    client._select_flow(FlowKind.INCIDENT)
    operations = _capture_operation(client)
    client.prompt_input.insert("1.0", "订单页面 /orders/42 一直停留在处理中")

    client._send_message()
    outcome = operations[0]()

    assert outcome.status == IncidentStatus.NEEDS_INPUT
    assert incident_application.calls == [
        (
            "start",
            str(tmp_path),
            "订单页面 /orders/42 一直停留在处理中",
            "",
            "ace-test",
        )
    ]


def test_incident_screenshot_paste_attaches_image_and_allows_image_only_send(
    root: tk.Toplevel,
    tmp_path: Path,
) -> None:
    image = tmp_path / "attachment" / "incident-screenshot.png"
    image.parent.mkdir()
    image.write_bytes(b"png-image")
    attachment = MessageAttachment(
        path=str(image),
        name=image.name,
        media_type="image/png",
        size_bytes=image.stat().st_size,
    )

    class FakeAttachmentStore:
        def capture_clipboard_image(self) -> MessageAttachment:
            return attachment

        def prepare_for_send(self, candidate: MessageAttachment) -> MessageAttachment:
            return candidate

    incident_application = FakeIncidentApplication()
    client = DesktopClient(
        root,
        FakeApplication(),  # type: ignore[arg-type]
        incident_application,  # type: ignore[arg-type]
        workspace_service=FakeWorkspaceService(tmp_path),  # type: ignore[arg-type]
        attachment_store=FakeAttachmentStore(),  # type: ignore[arg-type]
    )
    client._select_flow(FlowKind.INCIDENT)
    operations = _capture_operation(client)

    assert client._on_prompt_paste(object()) == "break"  # type: ignore[arg-type]
    assert client.attachment_frame.winfo_manager() == "grid"
    assert "1 张异常截图" in client.attachment_status_var.get()
    client._send_message()
    outcome = operations[0]()

    assert outcome.status == IncidentStatus.NEEDS_INPUT
    assert incident_application.calls == [
        (
            "start",
            str(tmp_path),
            "请根据粘贴的异常界面截图定位并诊断问题。",
            "",
            "ace-test",
        )
    ]
    assert incident_application.attachments == [[attachment]]
    assert client.attachment_frame.winfo_manager() == ""


def test_development_text_paste_does_not_invoke_image_clipboard(
    root: tk.Toplevel,
) -> None:
    class FailingAttachmentStore:
        def capture_clipboard_image(self) -> MessageAttachment:
            raise AssertionError("development paste must keep Tk text behavior")

    client = DesktopClient(
        root,
        FakeApplication(),  # type: ignore[arg-type]
        attachment_store=FailingAttachmentStore(),  # type: ignore[arg-type]
    )

    assert client._on_prompt_paste(object()) is None  # type: ignore[arg-type]


def test_log_button_opens_application_log_directory(
    root: tk.Toplevel,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = FakeApplication()
    application.log_path = tmp_path / "logs" / "autocoding-agent.log"  # type: ignore[attr-defined]
    opened: list[Path] = []
    monkeypatch.setattr(
        "autocoding_agent.interfaces.desktop_ui.os.startfile",
        lambda path: opened.append(Path(path)),
    )
    client = DesktopClient(root, application)  # type: ignore[arg-type]

    client._open_log_directory()

    assert opened == [tmp_path / "logs"]


def test_approval_buttons_route_exact_session_and_rejection_reason(
    root: tk.Toplevel, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(AgentStatus.APPROVAL_REQUIRED)
    session.version = 4
    session.pending_approval = ApprovalRequest(
        scope=ApprovalScope.MODIFY,
        reason="需要修改目标文件",
        proposed_actions=["编辑 src/order.py"],
        proposal=ChangeProposal(
            summary="修复重复扣减。",
            changes=[
                ProposedChange(
                    path="src/order.py",
                    area="库存扣减",
                    current="取消流程会再次扣减。",
                    proposed="取消流程只恢复已扣减库存。",
                )
            ],
            expected_result="取消订单后库存准确。",
            preview_markdown="取消前 9 -> 取消后 10",
        ),
    )
    session.pending_approval_snapshot = snapshot_approval(session, session.pending_approval)
    application = FakeApplication([session])
    client = DesktopClient(root, application)  # type: ignore[arg-type]
    client.session_id = session.id
    client._render_session(session)
    root.update_idletasks()
    operations = _capture_operation(client)

    assert client.approval_text.yview()[0] == 0.0
    displayed = client.approval_text.get("1.0", "end")
    assert "1. 库存扣减\n\n文件：src/order.py\n\n修改前\n\n取消流程" in displayed
    assert client.approval_text.tag_ranges("chat_subheading")
    assert client.approval_text.tag_ranges("chat_inline_code")
    assert client.approval_text.tag_ranges("chat_bold")

    client._approve()
    # 排队期间另一个窗口更新方案，不得把批准悄悄改为新版本。
    session.version = 5
    client._show_approval(session)
    operations.pop(0)()
    monkeypatch.setattr(
        "autocoding_agent.interfaces.desktop_ui.simpledialog.askstring",
        lambda *_args, **_kwargs: "先只给方案",
    )
    client._reject()
    operations.pop(0)()

    assert application.calls == [
        ("approve", session.id),
        ("reject", session.id, "先只给方案"),
    ]
    assert application.approval_versions == [4]


def test_busy_window_waits_for_safe_close(root: tk.Toplevel) -> None:
    client = DesktopClient(root, FakeApplication())  # type: ignore[arg-type]
    client._busy = True

    client._on_close()

    assert root.winfo_exists()
    assert client._close_after_stop
    assert "等待当前操作" in client.status_var.get()


def test_incident_summary_details_and_manual_repair_entry(
    root: tk.Toplevel, tmp_path: Path, monkeypatch,
):
    class RepairApplication(FakeApplication):
        def start_incident_remediation(self, incident, *, progress_sink=None):
            self.calls.append(("repair", incident.id))
            return self.start(incident.workspace, "只读准备修复方案", incident.project)

    incident = IncidentSession(
        workspace=str(tmp_path), problem="订单停留处理中", status=IncidentStatus.COMPLETED,
        last_decision=IncidentDecision(
            status=IncidentStatus.COMPLETED, message="订单未完成履约。",
            diagnosis="事件消费失败，原因仍待现场证据确认。",
            recommended_actions=["核对消费失败后修复重试逻辑。"],
            findings=[IncidentFinding(summary="内部证据详情")], confidence=0.7,
        ),
        messages=[
            ChatMessage(role=MessageRole.USER, content="订单停留处理中"),
            ChatMessage(role=MessageRole.SYSTEM, content="内部查询过程"),
            ChatMessage(role=MessageRole.ASSISTANT, content="历史冗长诊断内容"),
        ],
    )
    application = RepairApplication()
    incidents = FakeIncidentApplication([incident])
    client = DesktopClient(
        root, application, incidents,  # type: ignore[arg-type]
        workspace_service=FakeWorkspaceService(tmp_path),  # type: ignore[arg-type]
    )
    client._select_flow(FlowKind.INCIDENT)
    client.session_id = incident.id
    client._render_incident_session(incident)
    transcript = client.transcript.get("1.0", "end")
    assert "异常总结" in transcript and "解决方案" in transcript
    assert "内部证据详情" not in transcript and "内部查询过程" not in transcript
    assert "历史冗长诊断内容" not in transcript
    assert client.remediation_button.winfo_manager() == "grid"
    assert not application.calls
    client._toggle_incident_details()
    transcript = client.transcript.get("1.0", "end")
    assert "内部证据详情" in transcript and "内部查询过程" in transcript
    client._toggle_incident_details()
    assert "内部证据详情" not in client.transcript.get("1.0", "end")
    operations = []
    monkeypatch.setattr(DesktopClient, "_run_in_background",
                        lambda page, operation, label: operations.append((page, operation)))
    client.remediation_button.invoke()
    assert client.flow == FlowKind.INCIDENT and client.session_id == incident.id
    assert len(operations) == 1 and not application.calls
    repair, operation = operations[0]
    assert repair is not client and repair.flow == FlowKind.DEVELOPMENT
    outcome = operation()
    assert application.calls[0] == ("repair", incident.id)
    repair.session_id = outcome.session_id
    repair._render_session(application.get_session(outcome.session_id))
    assert repair.remediation_button.winfo_manager() == ""
    assert not any(call[0] == "approve" for call in application.calls)
    client._show_conversation()
    assert client._visible_client is client
    assert client.session_id == incident.id
    assert incidents.calls == []


@pytest.mark.parametrize("state", ["input", "page_only", "cancelled", "no_solution"])
def test_incident_repair_button_only_for_completed_diagnosis(
    root: tk.Toplevel, tmp_path: Path, state: str,
):
    incident = IncidentSession(
        workspace=str(tmp_path), problem="异常", status=IncidentStatus.COMPLETED,
        last_decision=IncidentDecision(
            status=IncidentStatus.COMPLETED, message="总结", diagnosis="原因待核对",
            recommended_actions=["核对代码后制定方案"],
        ),
    )
    if state == "input":
        incident.status = IncidentStatus.NEEDS_INPUT
        incident.task_state = TaskState.WAITING_INPUT
    elif state == "page_only":
        incident.last_decision.completion_kind = IncidentCompletionKind.PAGE_LOCATION
    elif state == "cancelled":
        incident.task_state = TaskState.CANCELLED
    else:
        incident.last_decision.recommended_actions = []
    application = FakeApplication()
    client = DesktopClient(
        root, application, FakeIncidentApplication([incident]),  # type: ignore[arg-type]
        workspace_service=FakeWorkspaceService(tmp_path),  # type: ignore[arg-type]
    )
    client._select_flow(FlowKind.INCIDENT)
    client.session_id = incident.id
    client._render_incident_session(incident)
    assert client.remediation_button.winfo_manager() == ""
    operations = _capture_operation(client)
    client._start_incident_remediation()
    assert not operations and not application.calls


def test_stop_button_records_once_and_waits_for_worker(root, monkeypatch):
    session = _session(AgentStatus.NEEDS_INPUT)
    application = FakeApplication([session])
    requests = []
    monkeypatch.setattr(application, "request_stop", requests.append, raising=False)
    client = DesktopClient(root, application)
    client.session_id = session.id
    client._active_task_id = session.id
    client._task_operation = True
    client._set_busy(True, "running")
    assert client.stop_button.cget("state") == "normal"
    client._request_stop()
    client._request_stop()
    assert requests == [session.id]
    assert client._busy
    assert client.stop_button.cget("text") == "正在停止"
    assert client.stop_button.cget("state") == "disabled"
    client._set_busy(False)
    assert client.stop_button.cget("state") == "disabled"



def test_new_page_is_available_while_parent_runs_and_keeps_queues_separate(root, tmp_path):
    first, second = _session(), _session()
    app = FakeApplication([first, second])
    client = DesktopClient(root, app, workspace_service=FakeWorkspaceService(tmp_path))
    client.session_id = first.id
    client._task_operation = True
    client._active_task_id = first.id
    client._set_busy(True, "任务一")
    child = client._open_task_window(session=second)
    assert child is not None and child is not client
    assert child.session_id == second.id
    assert isinstance(child.root, tk.Frame)
    assert child.root.winfo_toplevel() is root
    assert child._result_queue is not client._result_queue
    assert not child._busy and client._busy
    assert str(child.project_combo.cget("state")) == "readonly"
    assert client.new_window_button.cget("state") == "normal"
    assert client.new_window_button.cget("text").startswith("会话")
    assert client._visible_client is child
    assert child._open_task_window(session=second) is child
    assert len(client._window_group) == 2
    child._on_close()
    assert client._busy and root.winfo_exists()
    assert len(client._window_group) == 1


def test_bound_page_opens_new_task_without_replacing_its_session(root, tmp_path):
    session = _session()
    client = DesktopClient(root, FakeApplication([session]),
                           workspace_service=FakeWorkspaceService(tmp_path))
    child = client._open_task_window(session=session)
    child._new_task()
    assert child.session_id == session.id
    assert len(client._window_group) == 3
    assert client._window_group[-1].session_id is None
    assert str(client._window_group[-1].project_combo.cget("state")) == "readonly"


def test_close_primary_conversation_waits_without_stopping_other_page(root, tmp_path):
    sessions = [_session(), _session()]
    app = FakeApplication(sessions)
    stopped = []
    app.request_stop = stopped.append
    client = DesktopClient(root, app, workspace_service=FakeWorkspaceService(tmp_path))
    client.session_id = sessions[0].id
    client._render_session(sessions[0])
    child = client._open_task_window(session=sessions[1])
    for page in (client, child):
        page._task_operation, page._active_task_id = True, page.session_id
        page._set_busy(True, "运行中")
    client._close_conversation()
    assert stopped == [sessions[0].id]
    assert not client._close_after_stop and child._busy
    client._set_busy(False)
    client._drain_results()
    assert client.session_id is None and client.root.winfo_exists()
    assert child._busy and child.session_id == sessions[1].id
    assert client._visible_client is child
    child._set_busy(False)
    child._close_conversation()


def test_background_error_and_git_preview_wait_for_page_selection(root, tmp_path, monkeypatch):
    client = DesktopClient(root, FakeApplication(),
                           workspace_service=FakeWorkspaceService(tmp_path))
    child = client._open_task_window()
    errors, previews = [], []
    monkeypatch.setattr("autocoding_agent.interfaces.desktop_ui.messagebox.showerror",
                        lambda *args, **kwargs: errors.append(args))
    client._publish_git = previews.append
    client._result_queue.put(("error", RuntimeError("后台错误")))
    client._result_queue.put(("git_preview", {"commit": "review"}))
    client._drain_results()
    assert not errors and not previews and client._visible_client is child
    client._show_conversation()
    assert len(errors) == 1 and previews == [{"commit": "review"}]
    client._show_conversation()
    assert len(errors) == 1 and len(previews) == 1
    child._close_conversation()


def test_same_page_switch_preserves_draft_attachment_and_expanded_analysis(root, tmp_path):
    incidents = [IncidentSession(workspace=str(tmp_path), project="ace-test", problem=f"异常{i}",
                                 status=IncidentStatus.NEEDS_INPUT) for i in range(2)]
    incidents[0].messages = [ChatMessage(role=MessageRole.USER, content="检查订单异常")]
    incidents[0].events = [
        AgentEvent(type=EventType.TURN_STARTED, message="调查"),
        AgentEvent(type=EventType.RUNTIME_ACTIVITY, message="先核对订单入口",
                   data={"kind": "assistant_message"}),
    ]
    client = DesktopClient(root, FakeApplication(), FakeIncidentApplication(incidents),
                           workspace_service=FakeWorkspaceService(tmp_path))
    client._select_flow(FlowKind.INCIDENT)
    client.session_id = incidents[0].id
    client._render_incident_session(incidents[0])
    client.prompt_input.insert("1.0", "尚未发送的现场补充")
    image = tmp_path / "evidence.png"
    image.write_bytes(b"test attachment")
    attachment = MessageAttachment(path=str(image), name=image.name, media_type="image/png",
                                   size_bytes=image.stat().st_size)
    client._pending_attachments.append(attachment)
    client._refresh_attachment_display()
    trace_key = next(iter(client._chat_renderer.analysis_sections))
    client._chat_renderer.toggle("analysis:" + trace_key)
    child = client._open_task_window(session=incidents[1], flow=FlowKind.INCIDENT)
    child.prompt_input.insert("1.0", "第二个会话的草稿")
    assert client._visible_client is child and child.root.winfo_manager() == "place"
    assert not child._pending_attachments
    client._show_conversation()
    assert client._visible_client is client and not child.root.winfo_manager()
    assert client.prompt_input.get("1.0", "end").strip() == "尚未发送的现场补充"
    assert client._pending_attachments == [attachment]
    assert "analysis:" + trace_key in client._chat_renderer.expanded
    assert "收起分析过程" in client.transcript.get("1.0", "end")
    child._show_conversation()
    assert child.prompt_input.get("1.0", "end").strip() == "第二个会话的草稿"
    child._on_close()
    assert client._visible_client is client and root.winfo_exists()


def test_running_task_can_open_history_and_new_page_without_replacing_identity(root, tmp_path):
    first, second = _session(), _session()
    for session in (first, second):
        session.workspace, session.project = str(tmp_path), "ace-test"
    app = FakeApplication([first, second])
    client = DesktopClient(root, app, workspace_service=FakeWorkspaceService(tmp_path))
    client.session_id = first.id
    client._render_session(first)
    client._task_operation, client._active_task_id = True, first.id
    client._set_busy(True, "正在调查 A")
    assert str(client.sessions_list.cget("state")) == "normal"
    assert str(client.history_search.cget("state")) == "normal"
    assert client.new_task_button.cget("state") == "normal"
    client.sessions_list.selection_set(client._session_ids.index(second.id))
    client._select_session(None)
    child = client._visible_client
    assert child is not client and child.session_id == second.id
    assert client.session_id == first.id and client._active_task_id == first.id and client._busy
    assert client._open_task_window(session=first) is client
    assert client._visible_client is client and len(client._window_group) == 2
    client._new_task()
    assert client._visible_client.session_id is None and len(client._window_group) == 3
    assert client._busy and client.session_id == first.id
    client._set_busy(False)


def test_approval_display_and_version_stay_with_each_conversation(root, tmp_path):
    sessions = [_session(AgentStatus.APPROVAL_REQUIRED) for _ in range(2)]
    for i, session in enumerate(sessions):
        session.version = i + 5
        session.pending_approval = ApprovalRequest(
            scope=ApprovalScope.VERIFY, reason=f"方案 {i}", proposed_actions=[f"验证目标 {i}"],
        )
        session.pending_approval_snapshot = snapshot_approval(session, session.pending_approval)
    app = FakeApplication(sessions)
    client = DesktopClient(root, app, workspace_service=FakeWorkspaceService(tmp_path))
    client.session_id = sessions[0].id
    client._render_session(sessions[0])
    client.collapse_approval_button.invoke()
    original_text = client.approval_text.get("1.0", "end")
    child = client._open_task_window(session=sessions[1])
    assert "验证目标 1" in child.approval_text.get("1.0", "end")
    assert child._reviewed_approval_version == 6
    child_calls, parent_calls = _capture_operation(child), _capture_operation(client)
    child._approve()
    client._show_conversation()
    assert client._reviewed_approval_version == 5
    assert client.approval_text.get("1.0", "end") == original_text
    assert not client._review_reading
    client._approve()
    assert len(child_calls) == len(parent_calls) == 1
    parent_calls[0]()
    child_calls[0]()
    assert app.calls == [("approve", sessions[0].id), ("approve", sessions[1].id)]
    assert app.approval_versions == [5, 6]


def test_two_pages_run_concurrently_and_child_stop_does_not_stop_parent(root, tmp_path):
    import threading
    import time

    sessions = [_session(), _session()]
    app = FakeApplication(sessions)
    stopped = []
    app.request_stop = stopped.append
    client = DesktopClient(root, app, workspace_service=FakeWorkspaceService(tmp_path))
    client.session_id = sessions[0].id
    child = client._open_task_window(session=sessions[1])
    ready = [threading.Event(), threading.Event()]
    release = threading.Event()
    completed = [threading.Event(), threading.Event()]

    def operation(index):
        ready[index].set()
        release.wait(timeout=3)
        session = sessions[index]
        completed[index].set()
        return AgentOutcome(session_id=session.id, workspace=session.workspace,
                            status=AgentStatus.NEEDS_INPUT, message="已完成调查")

    try:
        client._run_in_background(lambda: operation(0), "任务一")
        child._run_in_background(lambda: operation(1), "任务二")
        assert all(event.wait(timeout=1) for event in ready)
        client._show_conversation()
        assert client._visible_client is client and client._busy and child._busy
        child._show_conversation()
        assert client._visible_client is child and client._busy and child._busy
        child._on_close()
        assert stopped == [sessions[1].id]
        assert child._close_after_stop and client._busy
        release.set()
        assert all(event.wait(timeout=1) for event in completed)
        deadline = time.monotonic() + 3
        while (client._busy or not child._closed) and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        assert child._closed
        assert client.session_id == sessions[0].id
        assert client.root.winfo_exists()
    finally:
        release.set()


def test_primary_close_coordinates_children_and_cancels_only_their_callbacks(root, tmp_path):
    sessions = [_session(), _session()]
    app = FakeApplication(sessions)
    stopped = []
    app.request_stop = stopped.append
    client = DesktopClient(root, app, workspace_service=FakeWorkspaceService(tmp_path))
    child = client._open_task_window(session=sessions[1])
    child._task_operation = True
    child._active_task_id = sessions[1].id
    child._set_busy(True, "任务二")
    client._on_close()
    assert stopped == [sessions[1].id]
    assert root.winfo_exists() and client._close_after_stop
    child._set_busy(False)
    child._drain_results()
    assert child._closed
    client._drain_results()
    assert client._closed


def test_stop_before_task_id_waits_for_first_progress_and_ignores_other_task(root, tmp_path):
    app = FakeApplication()
    stopped = []
    app.request_stop = stopped.append
    client = DesktopClient(root, app, workspace_service=FakeWorkspaceService(tmp_path))
    client._task_operation = True
    client._set_busy(True, "准备")
    client._request_stop()
    assert client._stop_when_ready and not stopped
    client._present_progress(ProgressEvent.for_phase(
        ProgressWorkflow.DEVELOPMENT, ProgressPhase.PREPARING_CONTEXT, task_id="task-a",
    ), immediate=True)
    assert stopped == ["task-a"]
    client._present_progress(ProgressEvent.for_phase(
        ProgressWorkflow.DEVELOPMENT, ProgressPhase.MODIFYING_CODE, task_id="task-b",
    ), immediate=True)
    assert client._active_task_id == "task-a"
    assert stopped == ["task-a"]



def test_bound_incident_page_starts_remediation_in_separate_development_page(
    root, tmp_path, monkeypatch,
):
    incident = IncidentSession(
        workspace=str(tmp_path), problem="订单异常", status=IncidentStatus.COMPLETED,
        last_decision=IncidentDecision(
            status=IncidentStatus.COMPLETED, message="诊断完成", diagnosis="需要修改重试逻辑",
            recommended_actions=["修复重试分支"],
        ),
    )
    app = FakeApplication()
    client = DesktopClient(root, app, FakeIncidentApplication([incident]),
                           workspace_service=FakeWorkspaceService(tmp_path))
    child = client._open_task_window(session=incident, flow=FlowKind.INCIDENT)
    operations = []
    monkeypatch.setattr(DesktopClient, "_run_in_background",
                        lambda window, operation, label: operations.append((window, operation)))
    child._start_incident_remediation()
    assert child.flow == FlowKind.INCIDENT and child.session_id == incident.id
    assert len(operations) == 1
    repair_window = operations[0][0]
    assert repair_window is not child
    assert repair_window.flow == FlowKind.DEVELOPMENT
    assert repair_window.session_id is None
    assert not app.calls


def test_review_split_drag_collapse_and_refresh_preserve_reading(root: tk.Toplevel) -> None:
    session = _session(AgentStatus.APPROVAL_REQUIRED)
    session.pending_approval = ApprovalRequest(
        scope=ApprovalScope.VERIFY, reason="验证修改",
        proposed_actions=[f"验证第 {index} 个操作并记录结果。" for index in range(50)],
    )
    session.pending_approval_snapshot = snapshot_approval(session, session.pending_approval)
    application = FakeApplication([session])
    client = DesktopClient(root, application)  # type: ignore[arg-type]
    client.session_id = session.id
    root.geometry("1840x980")
    root.deiconify()
    client._render_session(session)
    root.update()
    panes = client.conversation_panes
    original = panes.sash_coord(0)[0]
    panes.event_generate("<ButtonPress-1>", x=original + 2, y=80)
    panes.event_generate("<B1-Motion>", x=original + 162, y=80)
    panes.event_generate("<ButtonRelease-1>", x=original + 162, y=80)
    root.update()
    moved = panes.sash_coord(0)[0]
    assert moved > original + 100
    ratio = client._review_split
    client.approval_text.yview_moveto(0.5)
    position = client.approval_text.yview()[0]
    client.collapse_approval_button.invoke()
    root.update()
    assert len(panes.panes()) == 1 and client.transcript_panel.winfo_ismapped()
    client._render_session(session)
    root.update()
    assert not client.approval_frame.winfo_ismapped()
    client.review_toggle.invoke()
    root.update()
    assert abs(panes.sash_coord(0)[0] - moved) <= 3
    assert abs(client.approval_text.yview()[0] - position) < 0.02
    client._set_busy(True, "正在处理")
    root.update()
    assert abs(client._review_split - ratio) < 0.01
    for width in (920, 1280, 1840):
        root.geometry(f"{width}x800")
        root.update()
        assert client.approval_frame.winfo_ismapped()
        assert client.approve_button.winfo_rootx() + client.approve_button.winfo_width() <= (
            root.winfo_rootx() + root.winfo_width()
        )
    assert abs(client._review_split - ratio) < 0.01
    assert not application.calls


def test_many_approval_issues_keep_actions_visible(root: tk.Toplevel) -> None:
    session = _session(AgentStatus.APPROVAL_REQUIRED)
    session.pending_approval = ApprovalRequest(
        scope=ApprovalScope.VERIFY, reason="验证修改", proposed_actions=["pytest"],
    )
    issues = tuple(f"第 {index} 项：" + "文件读取依据需要重新核对。" * 20 for index in range(12))
    session.pending_approval_snapshot = snapshot_approval(
        session, session.pending_approval,
    ).model_copy(update={"issues": issues})
    client = DesktopClient(root, FakeApplication([session]))  # type: ignore[arg-type]
    client.session_id = session.id
    root.geometry("1280x800")
    root.deiconify()
    client._render_session(session)
    root.update()
    assert len(client.approval_issue.cget("text")) < 340
    assert "第 11 项" in client.approval_text.get("1.0", "end")
    assert client.approval_text.winfo_height() > 150
    assert client.approve_button.winfo_rooty() + client.approve_button.winfo_height() < (
        client.composer_panel.winfo_rooty()
    )
