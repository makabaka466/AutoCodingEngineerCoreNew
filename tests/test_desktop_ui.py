from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from autocoding_agent.core.models import (
    AgentOutcome,
    AgentSession,
    AgentStatus,
    ApprovalRequest,
    ApprovalScope,
    ChangeProposal,
    ChatMessage,
    MessageRole,
    ProposedChange,
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
    format_approval_details,
    session_list_label,
)
from autocoding_agent.interfaces.system_settings_ui import SystemSettingsDialog
from autocoding_agent.model_setup import ClaudeInstallation, ModelSetupState
from autocoding_agent.sqlserver_config import (
    SQLServerAuthentication,
    SQLServerConfigState,
    SQLServerConnectionConfig,
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

    def send(self, session_id: str, message: str) -> AgentOutcome:
        self.calls.append(("send", session_id, message))
        session = self.sessions[session_id]
        return AgentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=session.status or AgentStatus.NEEDS_INPUT,
            message="继续处理",
        )

    def approve(self, session_id: str) -> AgentOutcome:
        self.calls.append(("approve", session_id))
        session = self.sessions[session_id]
        return AgentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=AgentStatus.COMPLETED,
            message="已完成",
        )

    def reject(self, session_id: str, reason: str = "") -> AgentOutcome:
        self.calls.append(("reject", session_id, reason))
        session = self.sessions[session_id]
        return AgentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=AgentStatus.NEEDS_INPUT,
            message="已拒绝",
        )


class FakeIncidentApplication:
    def __init__(self, sessions: list[IncidentSession] | None = None) -> None:
        self.sessions = {item.id: item for item in sessions or []}
        self.calls: list[tuple[str, ...]] = []

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
    ) -> IncidentOutcome:
        self.calls.append(
            ("start", str(workspace), problem, page_hint or "", project or "")
        )
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

    def send(self, session_id: str, message: str) -> IncidentOutcome:
        self.calls.append(("send", session_id, message))
        session = self.sessions[session_id]
        return IncidentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=IncidentStatus.NEEDS_INPUT,
            message="继续诊断",
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


def test_new_task_routes_first_message_to_application_start(
    root: tk.Toplevel, tmp_path: Path
) -> None:
    application = FakeApplication()
    client = DesktopClient(root, application)  # type: ignore[arg-type]
    operations = _capture_operation(client)
    client.workspace_var.set(str(tmp_path))
    client.prompt_input.insert("1.0", "调查 src/app.py 的报错")

    client._send_message()
    outcome = operations[0]()

    assert application.calls == [
        ("start", str(tmp_path), "调查 src/app.py 的报错", "生物")
    ]
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


def test_completed_task_locks_composer_but_new_task_restores_it(root: tk.Toplevel) -> None:
    session = _session(AgentStatus.COMPLETED)
    client = DesktopClient(root, FakeApplication([session]))  # type: ignore[arg-type]
    client.session_id = session.id

    client._render_session(session)
    assert client.prompt_input.cget("state") == "disabled"
    assert client.workspace_entry.cget("state") == "disabled"

    client._new_task()
    assert client.prompt_input.cget("state") == "normal"
    assert client.workspace_entry.cget("state") == "normal"


def test_busy_state_blocks_conflicting_controls(root: tk.Toplevel) -> None:
    client = DesktopClient(root, FakeApplication())  # type: ignore[arg-type]

    client._set_busy(True, "处理中")

    assert client.send_button.cget("state") == "disabled"
    assert client.new_task_button.cget("state") == "disabled"
    assert client.browse_button.cget("state") == "disabled"
    assert client.sessions_list.cget("state") == "disabled"
    assert client.model_config_button.cget("state") == "disabled"


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

    class FakeModelService:
        def inspect(self, _command: str | None = None) -> ModelSetupState:
            return model_state

    class FakeDatabaseService:
        def inspect(self) -> SQLServerConfigState:
            return database_state

        def drivers(self) -> list[str]:
            return ["ODBC Driver 17 for SQL Server"]

    knowledge_service = MarkdownKnowledgeService(tmp_path / "state")
    knowledge_service.create_branch(KnowledgeDomain.DEVELOPMENT, "生物")
    dialog = SystemSettingsDialog(
        root,
        FakeModelService(),  # type: ignore[arg-type]
        FakeDatabaseService(),  # type: ignore[arg-type]
        knowledge_service=knowledge_service,
    )

    assert [dialog.notebook.tab(tab, "text") for tab in dialog.notebook.tabs()] == [
        "模型与 Claude Code",
        "SQL Server",
        "MD 能力配置",
    ]
    root.update_idletasks()
    assert dialog.model_save_button.winfo_manager() == "pack"
    assert dialog.model_key_var.get() == ""
    assert dialog.model_key_entry.cget("show") != ""
    assert "留空保持不变" in dialog.model_key_hint_var.get()
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


def test_light_theme_and_workspace_row_are_part_of_composer(root: tk.Toplevel) -> None:
    client = DesktopClient(root, FakeApplication())  # type: ignore[arg-type]

    assert COLORS["window"] == "#F7F8FA"
    assert client.workspace_entry.master.master == client.composer_frame
    assert client.send_button.cget("background") == COLORS["accent"]
    assert client.send_button.cget("foreground") == "#FFFFFF"
    assert client.browse_button.cget("background") == COLORS["panel"]


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
    assert client.incident_context_frame.winfo_manager() == ""
    assert client.project_var.get() == "生物"
    assert client.project_path_var.get() == "knowledge/development/生物/生物.md"

    client._select_flow(FlowKind.INCIDENT)

    assert client.flow == FlowKind.INCIDENT
    assert client.incident_flow_button.selected is True
    assert client.development_flow_button.selected is False
    assert client.incident_context_frame.winfo_manager() == "grid"
    assert "异常诊断" in client.new_task_button.cget("text")
    assert client.task_title_var.get() == "新异常诊断"
    assert client.page_hint_entry.winfo_manager() == "grid"
    assert not hasattr(client, "database_browse_button")
    assert client.project_var.get() == "生物"
    assert client.project_path_var.get() == "knowledge/incident/生物/生物.md"


def test_incident_flow_routes_problem_and_page_to_incident_application(
    root: tk.Toplevel,
    tmp_path: Path,
) -> None:
    incident_application = FakeIncidentApplication()
    client = DesktopClient(
        root,
        FakeApplication(),  # type: ignore[arg-type]
        incident_application,  # type: ignore[arg-type]
    )
    client._select_flow(FlowKind.INCIDENT)
    operations = _capture_operation(client)
    client.workspace_var.set(str(tmp_path))
    client.page_hint_var.set("/orders/42")
    client.prompt_input.insert("1.0", "订单一直停留在处理中")

    client._send_message()
    outcome = operations[0]()

    assert outcome.status == IncidentStatus.NEEDS_INPUT
    assert incident_application.calls == [
        ("start", str(tmp_path), "订单一直停留在处理中", "/orders/42", "生物")
    ]


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


def test_busy_window_refuses_to_close(
    root: tk.Toplevel, monkeypatch: pytest.MonkeyPatch
) -> None:
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
