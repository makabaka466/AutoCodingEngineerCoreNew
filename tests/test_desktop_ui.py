from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from autocoding_agent.config import Settings
from autocoding_agent.core.models import (
    AgentOutcome,
    AgentSession,
    AgentStatus,
    ApprovalRequest,
    ApprovalScope,
    ChangeProposal,
    ChatMessage,
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
from autocoding_agent.database_models import QueryObservation, QueryObservationStatus
from autocoding_agent.embedding_setup import (
    EmbeddingConnectionConfig,
    EmbeddingSetupState,
)
from autocoding_agent.incident.models import (
    IncidentOutcome,
    IncidentSession,
    IncidentStatus,
)
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
    WorkspaceConfig,
    WorkspaceConfigState,
)
from autocoding_agent.workspace_knowledge import KnowledgeDomain, MarkdownKnowledgeService


class FakeApplication:
    def __init__(self, sessions: list[AgentSession] | None = None) -> None:
        self.sessions = {item.id: item for item in sessions or []}
        self.calls: list[tuple[str, ...]] = []

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
        progress_sink: ProgressSink | None = None,
    ) -> AgentOutcome:
        self.calls.append(("approve", session_id))
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
    def __init__(self, path: Path) -> None:
        self.state = WorkspaceConfigState(
            config=WorkspaceConfig(path=str(path)),
            available=path.is_dir(),
        )

    def inspect(self) -> WorkspaceConfigState:
        return self.state

    def save(self, workspace: str | Path) -> WorkspaceConfigState:
        path = Path(workspace).resolve()
        self.state = WorkspaceConfigState(
            config=WorkspaceConfig(path=str(path)),
            available=path.is_dir(),
        )
        return self.state

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


def test_session_list_label_is_compact_and_includes_status() -> None:
    session = _session(AgentStatus.COMPLETED)

    label = session_list_label(session, max_length=12)

    assert "…" in label
    assert "已完成" in label
    assert "08-20" in label


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
        "src/ui.py · approval card",
        "现在：Only a one-line reason is shown.",
        "改成：Show current state",
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

    assert client.approve_button.cget("state") == "disabled"
    assert client.reject_button.cget("state") == "normal"
    assert "缺少修改方案" in client.approval_title.cget("text")


def test_recovery_state_shows_explicit_safe_choices(root: tk.Toplevel) -> None:
    session = _session(AgentStatus.FAILED)
    session.task_state = TaskState.RECOVERY_REQUIRED
    application = FakeApplication([session])
    client = DesktopClient(root, application)  # type: ignore[arg-type]
    client.session_id = session.id
    operations = _capture_operation(client)

    client._render_session(session)

    assert "恢复任务" in client.approval_title.cget("text")
    assert client.approve_button.cget("text") == "只读检查"
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

    assert application.calls == [("start", str(tmp_path), "调查 src/app.py 的报错", "生物")]
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
    assert client.prompt_input.cget("state") == "normal"
    assert client.send_button.cget("text") == "发送任务"
    assert "任务目标" in client.prompt_placeholder.cget("text")


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
    assert client.new_task_button.cget("state") == "disabled"
    assert client.sessions_list.cget("state") == "disabled"
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
        "SQL Server",
        "项目路径",
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
    assert dialog.knowledge_path_var.get() == "knowledge/development/生物/生物.md"
    dialog.knowledge_editor.delete("1.0", "end")
    dialog.knowledge_editor.insert("1.0", "# Updated guide\n")
    assert dialog._save_knowledge() is True
    assert "Updated guide" in knowledge_service.branch_path(
        KnowledgeDomain.DEVELOPMENT, "生物"
    ).read_text(encoding="utf-8")
    dialog.knowledge_domain_var.set("异常处理")
    dialog._refresh_knowledge()
    dialog.knowledge_new_branch_var.set("生物")
    dialog._add_knowledge_branch()
    assert dialog.knowledge_branch_var.get() == "生物"
    assert dialog.knowledge_path_var.get() == "knowledge/incident/生物/生物.md"
    dialog._close()


def test_light_theme_and_configured_workspace_keep_composer_compact(
    root: tk.Toplevel,
) -> None:
    client = DesktopClient(root, FakeApplication())  # type: ignore[arg-type]

    assert COLORS["window"] == "#EEF3FA"
    assert not hasattr(client, "workspace_entry")
    assert not hasattr(client, "browse_button")
    assert isinstance(client.send_button, RoundedButton)
    assert client.send_button.cget("background") == COLORS["accent"]
    assert client.send_button.cget("foreground") == "#FFFFFF"
    assert client.transcript.tag_cget("user_message", "background") == COLORS["accent_soft"]
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
    assert COLORS["progress_accent"] == "#667EEA"
    assert client.activity_frame.cget("background") == COLORS["progress_accent_soft"]


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
    assert client.project_path_var.get() == "knowledge/development/生物/生物.md"

    client._select_flow(FlowKind.INCIDENT)

    assert client.flow == FlowKind.INCIDENT
    assert client.incident_flow_button.selected is True
    assert client.development_flow_button.selected is False
    assert "异常诊断" in client.new_task_button.cget("text")
    assert client.task_title_var.get() == "新异常诊断"
    assert "页面标题或路径" in client.prompt_placeholder.cget("text")
    assert "先理解你的对话内容" in client.transcript.get("1.0", "end")
    assert not hasattr(client, "page_hint_entry")
    assert not hasattr(client, "database_browse_button")
    assert client.project_var.get() == "生物"
    assert client.project_path_var.get() == "knowledge/incident/生物/生物.md"


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
            "生物",
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
            "生物",
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
    application = FakeApplication([session])
    client = DesktopClient(root, application)  # type: ignore[arg-type]
    client.session_id = session.id
    client._render_session(session)
    root.update_idletasks()
    operations = _capture_operation(client)

    assert client.approval_text.yview()[0] == 0.0

    client._approve()
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


def test_busy_window_refuses_to_close(root: tk.Toplevel, monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    client = DesktopClient(root, FakeApplication())  # type: ignore[arg-type]
    client._busy = True
    monkeypatch.setattr(
        "autocoding_agent.interfaces.desktop_ui.messagebox.showwarning",
        lambda _title, message, **_kwargs: warnings.append(message),
    )

    client._on_close()

    assert root.winfo_exists()
    assert warnings and "仍在处理" in warnings[0]
