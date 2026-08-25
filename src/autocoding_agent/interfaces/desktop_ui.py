"""Native desktop chat client backed by the shared AgentApplication facade."""

from __future__ import annotations

import ctypes
import os
import queue
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from autocoding_agent.application import AgentApplication, build_application
from autocoding_agent.core.models import (
    AgentOutcome,
    AgentSession,
    AgentStatus,
    ApprovalRequest,
    ApprovalScope,
    MessageRole,
)
from autocoding_agent.core.recovery.models import RecoveryAction
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.incident.application import (
    IncidentApplication,
    build_incident_application,
)
from autocoding_agent.incident.models import (
    IncidentOutcome,
    IncidentSession,
    IncidentStatus,
)
from autocoding_agent.interfaces.system_settings_ui import SystemSettingsDialog
from autocoding_agent.model_setup import ClaudeModelSetupService, ModelSetupState
from autocoding_agent.sqlserver_config import SQLServerConfigState
from autocoding_agent.sqlserver_service import SQLServerConnectionService
from autocoding_agent.workspace_knowledge import (
    KnowledgeDomain,
    MarkdownKnowledgeService,
)

COLORS = {
    "window": "#F5F7FB",
    "sidebar": "#F8FAFC",
    "surface": "#FFFFFF",
    "surface_subtle": "#F8FAFC",
    "panel": "#F1F5F9",
    "panel_hover": "#E8EEF6",
    "input": "#FFFFFF",
    "border": "#E2E8F0",
    "border_strong": "#CBD5E1",
    "text": "#0F172A",
    "muted": "#64748B",
    "subtle": "#94A3B8",
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "accent_soft": "#EFF6FF",
    "accent_border": "#BFDBFE",
    "user": "#EFF6FF",
    "success": "#15803D",
    "success_soft": "#F0FDF4",
    "warning": "#B45309",
    "warning_soft": "#FFFBEB",
    "danger": "#DC2626",
    "danger_soft": "#FEF2F2",
}

STATUS_PRESENTATION: dict[AgentStatus | None, tuple[str, str]] = {
    None: ("就绪", COLORS["muted"]),
    AgentStatus.NEEDS_INPUT: ("等待补充", COLORS["warning"]),
    AgentStatus.QUERY_REQUIRED: ("查询数据", COLORS["accent"]),
    AgentStatus.APPROVAL_REQUIRED: ("等待授权", COLORS["warning"]),
    AgentStatus.COMPLETED: ("已完成", COLORS["success"]),
    AgentStatus.FAILED: ("失败", COLORS["danger"]),
}

INCIDENT_STATUS_PRESENTATION: dict[IncidentStatus, tuple[str, str]] = {
    IncidentStatus.NEEDS_INPUT: ("等待补充", COLORS["warning"]),
    IncidentStatus.QUERY_REQUIRED: ("查询数据", COLORS["accent"]),
    IncidentStatus.COMPLETED: ("已完成", COLORS["success"]),
    IncidentStatus.FAILED: ("失败", COLORS["danger"]),
}

STATUS_SURFACES: dict[AgentStatus | IncidentStatus | None, tuple[str, str]] = {
    None: (COLORS["surface_subtle"], COLORS["border"]),
    AgentStatus.NEEDS_INPUT: (COLORS["warning_soft"], "#FDE68A"),
    AgentStatus.QUERY_REQUIRED: (COLORS["accent_soft"], COLORS["accent_border"]),
    AgentStatus.APPROVAL_REQUIRED: (COLORS["warning_soft"], "#FDE68A"),
    AgentStatus.COMPLETED: (COLORS["success_soft"], "#BBF7D0"),
    AgentStatus.FAILED: (COLORS["danger_soft"], "#FECACA"),
    IncidentStatus.NEEDS_INPUT: (COLORS["warning_soft"], "#FDE68A"),
    IncidentStatus.QUERY_REQUIRED: (COLORS["accent_soft"], COLORS["accent_border"]),
    IncidentStatus.COMPLETED: (COLORS["success_soft"], "#BBF7D0"),
    IncidentStatus.FAILED: (COLORS["danger_soft"], "#FECACA"),
}


class FlowKind(StrEnum):
    DEVELOPMENT = "development"
    INCIDENT = "incident"


_INSTANCE_MUTEX_NAME = "Local\\AutoCodingEngineerDesktopClient"
_instance_mutex: int | None = None


def session_list_label(session: AgentSession, max_length: int = 22) -> str:
    """Return a compact, stable label for the recent-task list."""

    title = " ".join(session.goal.split()) or "未命名任务"
    if len(title) > max_length:
        title = f"{title[: max_length - 1]}…"
    status, _ = STATUS_PRESENTATION[session.status]
    return f"{title}\n{status} · {session.updated_at.astimezone():%m-%d %H:%M}"


def incident_session_list_label(session: IncidentSession, max_length: int = 22) -> str:
    """Return a compact label for an incident session."""

    title = " ".join(session.problem.split()) or "未命名异常"
    if len(title) > max_length:
        title = f"{title[: max_length - 1]}…"
    status = INCIDENT_STATUS_PRESENTATION[session.status][0] if session.status else "未开始"
    return f"{title}\n{status} · {session.updated_at.astimezone():%m-%d %H:%M}"


def format_approval_details(approval: ApprovalRequest) -> str:
    """Format the structured proposal for native-client review."""

    proposal = approval.proposal
    if proposal is None:
        lines = [approval.reason]
        if approval.proposed_actions:
            lines.extend(["", "计划操作", *[f"• {item}" for item in approval.proposed_actions]])
        return "\n".join(lines)

    lines = ["方案概述", proposal.summary, "", "修改内容"]
    for index, change in enumerate(proposal.changes, start=1):
        location = change.path or change.area
        if change.path and change.area != change.path:
            location = f"{change.path} · {change.area}"
        lines.extend(
            [
                f"{index}. {location}",
                f"   现在：{change.current}",
                f"   改成：{change.proposed}",
            ]
        )
    lines.extend(["", "目标效果", proposal.expected_result])
    preview_fallback = (
        "暂无可在实施前可靠呈现的预览，将按下面的验证计划确认最终效果。"
        if proposal.validation
        else "这项修改不适合在实施前生成可信预览，实施后再确认实际效果。"
    )
    lines.extend(["", "预览", proposal.preview_markdown or preview_fallback])
    if proposal.impact:
        lines.extend(["", "影响与边界", *[f"• {item}" for item in proposal.impact]])
    if proposal.validation:
        lines.extend(["", "验证计划", *[f"• {item}" for item in proposal.validation]])
    return "\n".join(lines)


class FlowPill(tk.Canvas):
    """A compact rounded flow selector with an explicit selected state."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        selected: bool = False,
    ) -> None:
        super().__init__(
            parent,
            width=102,
            height=38,
            bg=parent.cget("bg"),
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
            takefocus=1,
        )
        self._label = text
        self._command = command
        self._selected = selected
        self._enabled = True
        self.bind("<Button-1>", self._invoke)
        self.bind("<Return>", self._invoke)
        self.bind("<space>", self._invoke)
        self._redraw()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._redraw()

    @property
    def selected(self) -> bool:
        return self._selected

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._redraw()

    def _invoke(self, _event: tk.Event[tk.Misc]) -> None:
        if self._enabled:
            self._command()

    def _redraw(self) -> None:
        self.delete("all")
        fill = COLORS["accent"] if self._selected else COLORS["panel"]
        outline = COLORS["accent"] if self._selected else COLORS["border"]
        foreground = "#FFFFFF" if self._selected else COLORS["text"]
        if not self._enabled:
            fill = COLORS["panel"]
            foreground = "#98A2B3"
        self._rounded_rectangle(2, 2, 100, 36, radius=17, fill=fill, outline=outline)
        self.create_text(
            51,
            19,
            text=self._label,
            fill=foreground,
            font=("Microsoft YaHei UI", 9, "bold"),
        )

    def _rounded_rectangle(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        radius: int,
        **kwargs: object,
    ) -> None:
        points = [
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        ]
        self.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class RoundedButton(tk.Canvas):
    """Keyboard-accessible rounded button that keeps the small Tk dependency surface."""

    _OPTION_ALIASES = {
        "bg": "background",
        "fg": "foreground",
        "active_background": "activebackground",
        "activeforeground": "foreground",
    }

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        background: str,
        active_background: str,
        foreground: str,
        anchor: str = "center",
        width: int = 0,
    ) -> None:
        requested_width = width or max(82, len(text) * 14 + 30)
        super().__init__(
            parent,
            width=requested_width,
            height=38,
            bg=parent.cget("bg"),
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
            takefocus=1,
        )
        # Do not shadow tkinter.Misc._options(), which Canvas drawing relies on.
        self._button_options: dict[str, object] = {
            "text": text,
            "command": command,
            "background": background,
            "activebackground": active_background,
            "foreground": foreground,
            "disabledforeground": COLORS["subtle"],
            "state": "normal",
            "anchor": anchor,
        }
        self._hovered = False
        self._focused = False
        self.bind("<Configure>", lambda _event: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._invoke)
        self.bind("<Return>", self._invoke)
        self.bind("<space>", self._invoke)
        self.bind("<FocusIn>", self._on_focus)
        self.bind("<FocusOut>", self._on_focus)
        self._redraw()

    def configure(  # type: ignore[override]
        self,
        cnf: dict[str, object] | None = None,
        **kwargs: object,
    ) -> object:
        if cnf:
            kwargs = {**cnf, **kwargs}
        if not kwargs:
            return super().configure()
        canvas_options: dict[str, object] = {}
        for key, value in kwargs.items():
            normalized = self._OPTION_ALIASES.get(key, key)
            if normalized in self._button_options:
                self._button_options[normalized] = value
            else:
                canvas_options[key] = value
        if canvas_options:
            super().configure(**canvas_options)
        self.configure_cursor()
        self._redraw()
        return None

    config = configure

    def cget(self, key: str) -> object:  # type: ignore[override]
        normalized = self._OPTION_ALIASES.get(key, key)
        if normalized in self._button_options:
            return self._button_options[normalized]
        return super().cget(key)

    def invoke(self) -> None:
        if self._button_options["state"] == "normal":
            command = self._button_options["command"]
            if callable(command):
                command()

    def configure_cursor(self) -> None:
        self.configure_base(
            cursor="hand2" if self._button_options["state"] == "normal" else "arrow"
        )

    def configure_base(self, **kwargs: object) -> None:
        super().configure(**kwargs)

    def _on_enter(self, _event: tk.Event[tk.Misc]) -> None:
        self._hovered = True
        self._redraw()

    def _on_leave(self, _event: tk.Event[tk.Misc]) -> None:
        self._hovered = False
        self._redraw()

    def _on_focus(self, event: tk.Event[tk.Misc]) -> None:
        self._focused = event.type == tk.EventType.FocusIn
        self._redraw()

    def _invoke(self, _event: tk.Event[tk.Misc]) -> None:
        self.invoke()

    def _redraw(self) -> None:
        if not hasattr(self, "_button_options"):
            return
        self.delete("all")
        state = str(self._button_options["state"])
        fill = str(self._button_options["background"])
        foreground = str(self._button_options["foreground"])
        if state != "normal":
            fill = COLORS["panel"]
            foreground = str(self._button_options["disabledforeground"])
        elif self._hovered:
            fill = str(self._button_options["activebackground"])
        primary = self._button_options["background"] == COLORS["accent"]
        outline = COLORS["accent"] if self._focused else (
            fill if primary else COLORS["border_strong"]
        )
        canvas_width = max(self.winfo_width(), self.winfo_reqwidth())
        canvas_height = max(self.winfo_height(), 38)
        self._rounded_rectangle(
            2,
            2,
            canvas_width - 2,
            canvas_height - 2,
            radius=11,
            fill=fill,
            outline=outline,
            width=2 if self._focused else 1,
        )
        left_aligned = self._button_options["anchor"] == "w"
        self.create_text(
            16 if left_aligned else canvas_width / 2,
            canvas_height / 2,
            text=str(self._button_options["text"]),
            fill=foreground,
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w" if left_aligned else "center",
        )

    def _rounded_rectangle(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        radius: float,
        **kwargs: object,
    ) -> None:
        points = [
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        ]
        self.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class DesktopClient:
    """A small Codex-style desktop shell around the platform-neutral kernel."""

    def __init__(
        self,
        root: tk.Tk,
        application: AgentApplication | None = None,
        incident_application: IncidentApplication | None = None,
        setup_service: ClaudeModelSetupService | None = None,
        sqlserver_service: SQLServerConnectionService | None = None,
        knowledge_service: MarkdownKnowledgeService | None = None,
    ) -> None:
        self.root = root
        self.setup_service = setup_service or ClaudeModelSetupService()
        self.sqlserver_service = sqlserver_service or SQLServerConnectionService()
        self.knowledge_service = knowledge_service or MarkdownKnowledgeService()
        self._applications_injected = application is not None or incident_application is not None
        self._settings_dialog: SystemSettingsDialog | None = None
        self._active_development_database_reference: str | None = None
        self.application = (
            application
            if application is not None
            else self._build_current_development_application()
        )
        self._incident_application_injected = incident_application is not None
        self._active_incident_database_reference: str | None = None
        if incident_application is not None:
            self.incident_application = incident_application
        elif application is None:
            self.incident_application = self._build_current_incident_application()
        else:
            self.incident_application = None
        self.flow = FlowKind.DEVELOPMENT
        self.session_id: str | None = None
        self._flow_session_ids: dict[FlowKind, str | None] = {
            FlowKind.DEVELOPMENT: None,
            FlowKind.INCIDENT: None,
        }
        self._session_ids: list[str] = []
        self._result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._busy = False
        self._busy_label = ""
        self._busy_tick = 0
        self._current_status: AgentStatus | IncidentStatus | None = None
        self._approval_can_execute = True
        self._flow_projects: dict[FlowKind, str] = {
            FlowKind.DEVELOPMENT: "",
            FlowKind.INCIDENT: "",
        }

        self.workspace_var = tk.StringVar(value=str(Path.cwd()))
        self.project_var = tk.StringVar()
        self.project_path_var = tk.StringVar()
        self.page_hint_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.task_title_var = tk.StringVar(value="新开发任务")
        self.flow_caption_var = tk.StringVar(value="开发流程 · AI 工程工作台")

        self._configure_window()
        self._build_layout()
        self._refresh_flow_presentation()
        self._load_recent_sessions()
        self._render_welcome()
        self.root.after(100, self._drain_results)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_window(self) -> None:
        self.root.title("AutoCoding Engineer")
        self.root.configure(bg=COLORS["window"])
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1240, max(860, screen_width - 48), screen_width - 16)
        height = min(820, max(620, screen_height - 80), screen_height - 48)
        left = max(0, (screen_width - width) // 2)
        top = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.root.minsize(min(860, width), min(620, height))

    def _build_layout(self) -> None:
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_conversation_area()

    def _build_sidebar(self) -> None:
        sidebar = tk.Frame(
            self.root,
            bg=COLORS["sidebar"],
            width=268,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(3, weight=1)
        sidebar.grid_columnconfigure(0, weight=1)

        brand = tk.Frame(sidebar, bg=COLORS["sidebar"])
        brand.grid(row=0, column=0, sticky="ew", padx=18, pady=(22, 18))
        tk.Label(
            brand,
            text="AC",
            font=("Segoe UI", 11, "bold"),
            fg=COLORS["accent"],
            bg=COLORS["accent_soft"],
            width=3,
            height=2,
        ).pack(side="left", anchor="n")
        brand_copy = tk.Frame(brand, bg=COLORS["sidebar"])
        brand_copy.pack(side="left", padx=(10, 0), fill="x", expand=True)
        tk.Label(
            brand_copy,
            text="AutoCoding",
            font=("Microsoft YaHei UI", 10, "bold"),
            fg=COLORS["text"],
            bg=COLORS["sidebar"],
        ).pack(anchor="w")
        tk.Label(
            brand_copy,
            text="ENGINEERING AGENT",
            font=("Segoe UI", 7, "bold"),
            fg=COLORS["subtle"],
            bg=COLORS["sidebar"],
        ).pack(anchor="w", pady=(2, 0))

        self.new_task_button = self._button(
            sidebar,
            "新建任务",
            self._new_task,
            background=COLORS["panel"],
            active_background=COLORS["panel_hover"],
            anchor="w",
        )
        self.new_task_button.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 20))

        tk.Label(
            sidebar,
            text="最近任务",
            font=("Microsoft YaHei UI", 8, "bold"),
            fg=COLORS["muted"],
            bg=COLORS["sidebar"],
        ).grid(row=2, column=0, sticky="w", padx=18, pady=(2, 8))

        sessions_frame = tk.Frame(sidebar, bg=COLORS["sidebar"])
        sessions_frame.grid(row=3, column=0, sticky="nsew", padx=(10, 5), pady=(0, 10))
        sessions_frame.grid_rowconfigure(0, weight=1)
        sessions_frame.grid_columnconfigure(0, weight=1)
        self.sessions_list = tk.Listbox(
            sessions_frame,
            bg=COLORS["sidebar"],
            fg=COLORS["text"],
            selectbackground=COLORS["accent_soft"],
            selectforeground=COLORS["text"],
            borderwidth=0,
            highlightthickness=0,
            font=("Microsoft YaHei UI", 9),
            activestyle="none",
            exportselection=False,
            selectborderwidth=0,
        )
        self.sessions_list.grid(row=0, column=0, sticky="nsew")
        self.sessions_list.bind("<<ListboxSelect>>", self._select_session)
        sidebar_scroll = ttk.Scrollbar(
            sessions_frame, orient="vertical", command=self.sessions_list.yview
        )
        sidebar_scroll.grid(row=0, column=1, sticky="ns")
        self.sessions_list.configure(yscrollcommand=sidebar_scroll.set)

        self.model_config_button = self._button(
            sidebar,
            "系统配置",
            lambda: self._open_system_settings("model"),
            background=COLORS["panel"],
            active_background=COLORS["panel_hover"],
            anchor="w",
        )
        self.model_config_button.grid(row=4, column=0, sticky="ew", padx=14, pady=(2, 6))

        self.log_button = self._button(
            sidebar,
            "本地日志",
            self._open_log_directory,
            background=COLORS["panel"],
            active_background=COLORS["panel_hover"],
            anchor="w",
        )
        self.log_button.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 8))

        tk.Label(
            sidebar,
            text="本地运行 · 可审计 · 可恢复",
            font=("Microsoft YaHei UI", 8),
            fg=COLORS["muted"],
            bg=COLORS["sidebar"],
        ).grid(row=6, column=0, sticky="w", padx=16, pady=(5, 15))

    def _build_conversation_area(self) -> None:
        main = tk.Frame(self.root, bg=COLORS["window"])
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        header = tk.Frame(main, bg=COLORS["surface"], height=84)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        title_group = tk.Frame(header, bg=COLORS["surface"])
        title_group.grid(row=0, column=0, sticky="w", padx=(32, 12), pady=(14, 12))
        tk.Label(
            title_group,
            textvariable=self.flow_caption_var,
            font=("Segoe UI", 8, "bold"),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_group,
            textvariable=self.task_title_var,
            font=("Microsoft YaHei UI", 15, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))
        flow_selector = tk.Frame(header, bg=COLORS["surface"])
        flow_selector.grid(row=0, column=1, padx=(12, 18), pady=12)
        self.development_flow_button = FlowPill(
            flow_selector,
            "开发",
            lambda: self._select_flow(FlowKind.DEVELOPMENT),
            selected=True,
        )
        self.development_flow_button.grid(row=0, column=0, padx=(0, 5))
        self.incident_flow_button = FlowPill(
            flow_selector,
            "异常处理",
            lambda: self._select_flow(FlowKind.INCIDENT),
        )
        self.incident_flow_button.grid(row=0, column=1)
        self.status_badge = tk.Label(
            header,
            text="就绪",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg=COLORS["muted"],
            bg=COLORS["surface_subtle"],
            padx=12,
            pady=7,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        self.status_badge.grid(row=0, column=2, sticky="e", padx=(0, 30), pady=12)
        tk.Frame(main, bg=COLORS["border"], height=1).grid(row=0, column=0, sticky="sew")

        transcript_frame = tk.Frame(main, bg=COLORS["window"])
        transcript_frame.grid(row=1, column=0, sticky="nsew", padx=(48, 30), pady=(18, 0))
        transcript_frame.grid_rowconfigure(0, weight=1)
        transcript_frame.grid_columnconfigure(0, weight=1)
        self.transcript = tk.Text(
            transcript_frame,
            height=1,
            bg=COLORS["window"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            wrap="word",
            padx=14,
            pady=12,
            spacing1=2,
            spacing3=6,
            font=("Microsoft YaHei UI", 10),
            cursor="arrow",
        )
        self.transcript.grid(row=0, column=0, sticky="nsew")
        transcript_scroll = ttk.Scrollbar(
            transcript_frame, orient="vertical", command=self.transcript.yview
        )
        transcript_scroll.grid(row=0, column=1, sticky="ns")
        self.transcript.configure(yscrollcommand=transcript_scroll.set)
        self.transcript.tag_configure(
            "user_name",
            foreground="#475467",
            font=("Microsoft YaHei UI", 9, "bold"),
            lmargin1=16,
            lmargin2=16,
            rmargin=72,
            spacing1=14,
        )
        self.transcript.tag_configure(
            "assistant_name",
            foreground=COLORS["accent"],
            font=("Microsoft YaHei UI", 9, "bold"),
            lmargin1=16,
            lmargin2=16,
            rmargin=56,
            spacing1=14,
        )
        self.transcript.tag_configure(
            "system_name",
            foreground=COLORS["warning"],
            font=("Microsoft YaHei UI", 9, "bold"),
            lmargin1=16,
            lmargin2=16,
            rmargin=56,
            spacing1=14,
        )
        self.transcript.tag_configure(
            "message", foreground=COLORS["text"], font=("Microsoft YaHei UI", 10)
        )
        self.transcript.tag_configure(
            "user_message",
            foreground=COLORS["text"],
            background=COLORS["accent_soft"],
            font=("Microsoft YaHei UI", 10),
            lmargin1=16,
            lmargin2=16,
            rmargin=72,
            spacing1=7,
            spacing3=9,
        )
        self.transcript.tag_configure(
            "assistant_message",
            foreground=COLORS["text"],
            background=COLORS["surface"],
            font=("Microsoft YaHei UI", 10),
            lmargin1=16,
            lmargin2=16,
            rmargin=56,
            spacing1=7,
            spacing3=9,
        )
        self.transcript.tag_configure(
            "system_message",
            foreground=COLORS["text"],
            background=COLORS["warning_soft"],
            font=("Microsoft YaHei UI", 10),
            lmargin1=16,
            lmargin2=16,
            rmargin=56,
            spacing1=7,
            spacing3=9,
        )
        self.transcript.tag_configure(
            "muted", foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9)
        )
        self.transcript.tag_configure(
            "metadata",
            foreground=COLORS["text"],
            background=COLORS["surface_subtle"],
            lmargin1=16,
            lmargin2=16,
            rmargin=56,
            spacing1=9,
            spacing3=9,
        )
        self.transcript.configure(state="disabled")

        self.approval_frame = tk.Frame(
            main,
            bg=COLORS["warning_soft"],
            highlightthickness=1,
            highlightbackground="#FDE68A",
        )
        self.approval_frame.grid_columnconfigure(0, weight=1)
        self.approval_title = tk.Label(
            self.approval_frame,
            text="修改方案",
            anchor="w",
            font=("Microsoft YaHei UI", 10, "bold"),
            fg=COLORS["warning"],
            bg=COLORS["warning_soft"],
        )
        self.approval_title.grid(
            row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(12, 5)
        )
        self.approval_text = tk.Text(
            self.approval_frame,
            height=9,
            wrap="word",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["text"],
            bg=COLORS["warning_soft"],
            insertbackground=COLORS["text"],
            padx=10,
            pady=6,
        )
        self.approval_text.grid(row=1, column=0, sticky="nsew", padx=(8, 0))
        approval_scroll = ttk.Scrollbar(
            self.approval_frame, orient="vertical", command=self.approval_text.yview
        )
        approval_scroll.grid(row=1, column=1, sticky="ns", padx=(0, 8))
        self.approval_text.configure(yscrollcommand=approval_scroll.set, state="disabled")

        approval_actions = tk.Frame(self.approval_frame, bg=COLORS["warning_soft"])
        approval_actions.grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(7, 11)
        )
        approval_actions.grid_columnconfigure(0, weight=1)
        self.reject_button = self._button(
            approval_actions,
            "拒绝或调整",
            self._reject,
            background="#FEF3C7",
            active_background="#FDE68A",
        )
        self.reject_button.grid(row=0, column=1, padx=4)
        self.approve_button = self._button(
            approval_actions,
            "批准此方案",
            self._approve,
            background=COLORS["accent"],
            active_background=COLORS["accent_hover"],
        )
        self.approve_button.grid(row=0, column=2, padx=4)
        self.recovery_replan_button = self._button(
            approval_actions,
            "重新规划",
            self._replan_recovery,
            background="#E8EEF9",
            active_background="#D8E4F7",
        )

        self.composer_frame = tk.Frame(
            main,
            bg=COLORS["input"],
            highlightthickness=1,
            highlightbackground=COLORS["border_strong"],
            highlightcolor=COLORS["accent"],
        )
        self.composer_frame.grid(row=3, column=0, sticky="ew", padx=48, pady=(16, 10))
        self.composer_frame.grid_columnconfigure(0, weight=1)

        composer_header = tk.Frame(self.composer_frame, bg=COLORS["input"])
        composer_header.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        composer_header.grid_columnconfigure(0, weight=1)
        tk.Label(
            composer_header,
            text="任务上下文",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg=COLORS["text"],
            bg=COLORS["input"],
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            composer_header,
            text="Agent 会先调查并给出方案，再申请修改权限",
            font=("Microsoft YaHei UI", 8),
            fg=COLORS["muted"],
            bg=COLORS["input"],
        ).grid(row=0, column=1, sticky="e")

        project_row = tk.Frame(self.composer_frame, bg=COLORS["input"])
        project_row.grid(row=1, column=0, sticky="ew", padx=14, pady=(4, 4))
        project_row.grid_columnconfigure(2, weight=1)
        tk.Label(
            project_row,
            text="知识项目",
            font=("Microsoft YaHei UI", 8, "bold"),
            fg=COLORS["muted"],
            bg=COLORS["input"],
        ).grid(row=0, column=0, sticky="w", padx=(0, 9))
        self.project_combo = ttk.Combobox(
            project_row,
            textvariable=self.project_var,
            state="readonly",
            width=20,
            font=("Microsoft YaHei UI", 9),
        )
        self.project_combo.grid(row=0, column=1, sticky="w", ipady=5)
        self.project_combo.bind("<<ComboboxSelected>>", self._on_project_selected)
        tk.Label(
            project_row,
            textvariable=self.project_path_var,
            font=("Microsoft YaHei UI", 8),
            fg=COLORS["muted"],
            bg=COLORS["input"],
            anchor="w",
        ).grid(row=0, column=2, sticky="ew", padx=(12, 0))

        workspace_row = tk.Frame(self.composer_frame, bg=COLORS["input"])
        workspace_row.grid(row=2, column=0, sticky="ew", padx=14, pady=(2, 5))
        workspace_row.grid_columnconfigure(1, weight=1)
        tk.Label(
            workspace_row,
            text="项目路径",
            font=("Microsoft YaHei UI", 8, "bold"),
            fg=COLORS["muted"],
            bg=COLORS["input"],
        ).grid(row=0, column=0, sticky="w", padx=(0, 9))
        self.workspace_entry = tk.Entry(
            workspace_row,
            textvariable=self.workspace_var,
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["text"],
            bg=COLORS["surface_subtle"],
            insertbackground=COLORS["text"],
            selectbackground="#BFDBFE",
            selectforeground=COLORS["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
        )
        self.workspace_entry.grid(row=0, column=1, sticky="ew", ipady=6)
        self.browse_button = self._button(
            workspace_row,
            "浏览…",
            self._browse_workspace,
            background=COLORS["panel"],
            active_background=COLORS["panel_hover"],
        )
        self.browse_button.grid(row=0, column=2, padx=(8, 0), sticky="ns")

        self.incident_context_frame = tk.Frame(self.composer_frame, bg=COLORS["input"])
        self.incident_context_frame.grid(
            row=3,
            column=0,
            sticky="ew",
            padx=14,
            pady=(2, 4),
        )
        self.incident_context_frame.grid_columnconfigure(1, weight=1)
        tk.Label(
            self.incident_context_frame,
            text="异常页面",
            font=("Microsoft YaHei UI", 8, "bold"),
            fg=COLORS["muted"],
            bg=COLORS["input"],
        ).grid(row=0, column=0, sticky="w", padx=(0, 9), pady=(0, 4))
        self.page_hint_entry = tk.Entry(
            self.incident_context_frame,
            textvariable=self.page_hint_var,
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["text"],
            bg=COLORS["surface_subtle"],
            insertbackground=COLORS["text"],
            selectbackground="#BFDBFE",
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
        )
        self.page_hint_entry.grid(row=0, column=1, sticky="ew", ipady=6)

        self.prompt_input = tk.Text(
            self.composer_frame,
            height=4,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            padx=12,
            pady=10,
            undo=True,
        )
        tk.Frame(self.composer_frame, bg=COLORS["border"], height=1).grid(
            row=4, column=0, sticky="ew", padx=14, pady=(5, 0)
        )
        self.prompt_input.grid(row=5, column=0, sticky="ew", padx=2)
        self.prompt_input.bind("<Return>", self._on_return)
        self.prompt_input.bind("<Control-Return>", self._on_control_return)
        action_row = tk.Frame(self.composer_frame, bg=COLORS["input"])
        action_row.grid(row=6, column=0, sticky="ew", padx=12, pady=(0, 11))
        action_row.grid_columnconfigure(0, weight=1)
        tk.Label(
            action_row,
            text="Enter 发送 · Shift+Enter 换行 · 修改与验证会单独请求授权",
            font=("Microsoft YaHei UI", 8),
            fg=COLORS["muted"],
            bg=COLORS["input"],
        ).grid(row=0, column=0, sticky="w")
        self.send_button = self._button(
            action_row,
            "发送任务",
            self._send_message,
            background=COLORS["accent"],
            active_background=COLORS["accent_hover"],
        )
        self.send_button.grid(row=0, column=1, sticky="e")

        footer = tk.Frame(
            main,
            bg=COLORS["surface"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        footer.grid(row=4, column=0, sticky="ew", padx=48, pady=(0, 14))
        footer.grid_columnconfigure(0, weight=1)
        tk.Label(
            footer,
            textvariable=self.status_var,
            font=("Microsoft YaHei UI", 8),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(12, 8), pady=7)
        tk.Label(
            footer,
            text="本地执行 · 过程可追溯 · 中断可恢复",
            font=("Microsoft YaHei UI", 8),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
        ).grid(row=0, column=1, sticky="e", padx=(8, 12), pady=7)

    @staticmethod
    def _button(
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        background: str,
        active_background: str,
        anchor: str = "center",
        width: int = 0,
    ) -> RoundedButton:
        primary = background == COLORS["accent"]
        foreground = "#FFFFFF" if primary else COLORS["text"]
        return RoundedButton(
            parent,
            text=text,
            command=command,
            foreground=foreground,
            background=background,
            active_background=active_background,
            anchor=anchor,
            width=width,
        )

    def _select_flow(self, flow: FlowKind) -> None:
        if self._busy or flow == self.flow:
            return
        if flow == FlowKind.INCIDENT and self.incident_application is None:
            messagebox.showerror(
                "异常流程不可用",
                "当前客户端实例没有配置异常处理应用。",
                parent=self.root,
            )
            return
        self._flow_session_ids[self.flow] = self.session_id
        self.flow = flow
        self.session_id = self._flow_session_ids[flow]
        self._hide_approval()
        self._refresh_flow_presentation()
        self._load_recent_sessions(select_current=bool(self.session_id))
        if self.session_id:
            try:
                self._render_active_session(self._active_application().get_session(self.session_id))
            except Exception as exc:
                self.session_id = None
                self.status_var.set(f"读取会话失败：{exc}")
                self._render_welcome()
        else:
            self._render_welcome()
        self._sync_controls()

    def _refresh_flow_presentation(self) -> None:
        is_development = self.flow == FlowKind.DEVELOPMENT
        self.development_flow_button.set_selected(is_development)
        self.incident_flow_button.set_selected(not is_development)
        self.flow_caption_var.set(
            "开发流程 · AI 工程工作台"
            if is_development
            else "异常处理 · 页面与业务数据联合诊断"
        )
        self.new_task_button.configure(
            text="新建开发任务" if is_development else "新建异常诊断"
        )
        if is_development:
            self.incident_context_frame.grid_remove()
        else:
            self.incident_context_frame.grid()
        self._refresh_project_options()

    def _knowledge_domain(self) -> KnowledgeDomain:
        return (
            KnowledgeDomain.DEVELOPMENT
            if self.flow == FlowKind.DEVELOPMENT
            else KnowledgeDomain.INCIDENT
        )

    def _refresh_project_options(self) -> None:
        projects = self.knowledge_service.list_branches(self._knowledge_domain())
        self.project_combo.configure(values=projects)
        selected = self._flow_projects[self.flow]
        if selected not in projects:
            selected = projects[0] if projects else ""
        self.project_var.set(selected)
        self._flow_projects[self.flow] = selected
        self._refresh_project_path()

    def _on_project_selected(self, _event: tk.Event[tk.Misc]) -> None:
        self._flow_projects[self.flow] = self.project_var.get()
        self._refresh_project_path()

    def _refresh_project_path(self) -> None:
        project = self.project_var.get()
        if not project:
            self.project_path_var.set("请先在系统配置中添加项目")
            return
        path = self.knowledge_service.branch_path(self._knowledge_domain(), project)
        self.project_path_var.set(self.knowledge_service.relative_path(path))

    def _active_application(self) -> AgentApplication | IncidentApplication:
        if self.flow == FlowKind.DEVELOPMENT:
            return self.application
        if self.incident_application is None:
            raise RuntimeError("Incident application is not configured.")
        return self.incident_application

    def _render_active_session(self, session: AgentSession | IncidentSession) -> None:
        if self.flow == FlowKind.DEVELOPMENT:
            if not isinstance(session, AgentSession):
                raise TypeError("Development flow returned an invalid session.")
            self._render_session(session)
            return
        if not isinstance(session, IncidentSession):
            raise TypeError("Incident flow returned an invalid session.")
        self._render_incident_session(session)

    def _render_welcome(self) -> None:
        if self.flow == FlowKind.INCIDENT:
            self.task_title_var.set("新异常诊断")
            message = (
                "你好，我会协助定位和诊断应用异常。\n\n"
                "请填写项目路径，尽量提供异常页面、路由或标题。需要业务数据时会使用系统"
                "配置中的共用只读连接；当前流程不会修改文件或数据库。"
            )
        else:
            self.task_title_var.set("新开发任务")
            message = (
                "你好，我是 AutoCoding Engineer。\n\n"
                "选择项目目录，然后描述要调查、修改或验证的开发任务。"
                "如果需求还不够清楚，我会先向你确认最关键的信息。"
            )
        self._replace_transcript(
            [
                (
                    "assistant",
                    message,
                )
            ]
        )
        self._set_status(None)

    def _render_session(self, session: AgentSession) -> None:
        entries: list[tuple[str, str]] = []
        for item in session.messages:
            role = {
                MessageRole.USER: "user",
                MessageRole.ASSISTANT: "assistant",
                MessageRole.SYSTEM: "system",
            }[item.role]
            entries.append((role, item.content))

        decision = session.last_decision
        if decision is not None:
            details: list[str] = []
            if decision.evidence:
                details.append("依据")
                details.extend(
                    f"• {item.path + ': ' if item.path else ''}{item.summary}"
                    for item in decision.evidence
                )
            if decision.changed_files:
                details.append("变更文件")
                details.extend(f"• {item}" for item in decision.changed_files)
            if decision.test_summary:
                details.append(f"验证\n{decision.test_summary}")
            if session.query_observations:
                details.append("数据查询")
                details.extend(
                    f"• {item.query_name}: {item.returned_rows} 行"
                    + ("，已截断" if item.truncated else "")
                    for item in session.query_observations
                )
            if session.capability_document:
                details.append(f"能力文档\n{session.capability_document}")
            if details:
                entries.append(("metadata", "\n".join(details)))

        self._replace_transcript(entries)
        self.workspace_var.set(session.workspace)
        self.project_var.set(session.project or "")
        self._flow_projects[FlowKind.DEVELOPMENT] = session.project or ""
        self._refresh_project_path()
        title = " ".join(session.goal.split()) or "未命名任务"
        self.task_title_var.set(title[:64] + ("…" if len(title) > 64 else ""))
        self._set_status(session.status)
        self._show_approval(session)
        if session.status == AgentStatus.COMPLETED:
            self.status_var.set("任务已完成。点击左侧“新建任务”开始下一项工作。")
        elif session.status == AgentStatus.NEEDS_INPUT:
            self.status_var.set("请在下方补充模型询问的信息。")
        elif session.status == AgentStatus.APPROVAL_REQUIRED:
            self.status_var.set("请检查授权范围，再选择批准或拒绝。")
        elif session.status == AgentStatus.FAILED:
            if session.task_state in {TaskState.PAUSED, TaskState.RECOVERY_REQUIRED}:
                self.status_var.set("任务已安全暂停，请查看恢复报告并选择恢复方式。")
            elif session.task_state == TaskState.REPLANNING:
                self.status_var.set("验证未通过，请补充信息或继续只读分析以形成新方案。")
            else:
                self.status_var.set("任务执行失败，可以补充信息重试或新建任务。")
        self._sync_controls()

    def _render_incident_session(self, session: IncidentSession) -> None:
        entries: list[tuple[str, str]] = []
        for item in session.messages:
            role = {
                MessageRole.USER: "user",
                MessageRole.ASSISTANT: "assistant",
                MessageRole.SYSTEM: "system",
            }[item.role]
            entries.append((role, item.content))

        decision = session.last_decision
        if decision is not None:
            details: list[str] = []
            if decision.page:
                route = f" · {decision.page.route}" if decision.page.route else ""
                details.append(f"定位页面\n{decision.page.name}{route}")
                if decision.page.source_paths:
                    details.append("页面代码")
                    details.extend(f"• {item}" for item in decision.page.source_paths)
                if decision.page.related_paths:
                    details.append("关联代码")
                    details.extend(f"• {item}" for item in decision.page.related_paths)
            if decision.diagnosis:
                details.append(f"诊断\n{decision.diagnosis}")
            if decision.findings:
                details.append("发现")
                details.extend(f"• {item.summary}" for item in decision.findings)
            if session.query_observations:
                details.append("数据查询")
                details.extend(
                    f"• {item.query_name}: {item.returned_rows} 行"
                    + ("，已截断" if item.truncated else "")
                    for item in session.query_observations
                )
            if decision.recommended_actions:
                details.append("建议动作")
                details.extend(f"• {item}" for item in decision.recommended_actions)
            if decision.confidence is not None:
                details.append(f"置信度\n{decision.confidence:.0%}")
            if decision.automation_candidate:
                details.append("自动化候选\n适合后续评估钉钉自动处理。")
            if session.capability_document:
                details.append(f"异常能力文档\n{session.capability_document}")
            if details:
                entries.append(("metadata", "\n".join(details)))

        self._replace_transcript(entries)
        self.workspace_var.set(session.workspace)
        self.project_var.set(session.project or "")
        self._flow_projects[FlowKind.INCIDENT] = session.project or ""
        self._refresh_project_path()
        self.page_hint_var.set(session.page_hint or "")
        title = " ".join(session.problem.split()) or "未命名异常"
        self.task_title_var.set(title[:64] + ("…" if len(title) > 64 else ""))
        if session.task_state in {TaskState.PAUSED, TaskState.RECOVERY_REQUIRED}:
            self._show_incident_recovery(session)
        else:
            self._hide_approval()
        self._set_status(session.status)
        if session.status == IncidentStatus.COMPLETED:
            self.status_var.set("异常诊断已完成。可以切换流程或新建另一项诊断。")
        elif session.status == IncidentStatus.NEEDS_INPUT:
            self.status_var.set("请在下方补充模型询问的异常信息。")
        elif session.status == IncidentStatus.QUERY_REQUIRED:
            self.status_var.set("正在根据页面和只读数据继续诊断。")
        elif session.status == IncidentStatus.FAILED:
            if session.task_state in {TaskState.PAUSED, TaskState.RECOVERY_REQUIRED}:
                self.status_var.set("异常诊断已暂停，请选择继续只读调查、重新调查或取消。")
            else:
                self.status_var.set("异常诊断失败，可补充信息重试；详情请查看本地日志。")
        self._sync_controls()

    def _replace_transcript(self, entries: list[tuple[str, str]]) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        names = {"user": "你 · 任务", "assistant": "Agent · 回应", "system": "系统 · 提示"}
        for role, content in entries:
            if role == "metadata":
                self.transcript.insert("end", f"\n{content.strip()}\n", "metadata")
                continue
            self.transcript.insert("end", f"{names[role]}\n", f"{role}_name")
            self.transcript.insert("end", f"{content.strip()}\n", f"{role}_message")
            self.transcript.insert("end", "\n", "muted")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _load_recent_sessions(self, select_current: bool = False) -> None:
        try:
            sessions = self._active_application().list_sessions()
        except Exception as exc:
            self.status_var.set(f"读取历史任务失败：{exc}")
            return
        self.sessions_list.delete(0, "end")
        self._session_ids = [item.id for item in sessions]
        for session in sessions:
            # Listbox has no multiline row spacing, so keep one readable line per task.
            label = (
                session_list_label(session)
                if isinstance(session, AgentSession)
                else incident_session_list_label(session)
            )
            self.sessions_list.insert("end", label.replace("\n", "  "))
        if sessions and not self.session_id:
            self.workspace_var.set(sessions[0].workspace)
        if select_current and self.session_id in self._session_ids:
            index = self._session_ids.index(self.session_id)
            self.sessions_list.selection_clear(0, "end")
            self.sessions_list.selection_set(index)
            self.sessions_list.see(index)

    def _select_session(self, _event: tk.Event[tk.Misc]) -> None:
        if self._busy:
            return
        selection = self.sessions_list.curselection()
        if not selection:
            return
        session_id = self._session_ids[selection[0]]
        try:
            session = self._active_application().get_session(session_id)
        except Exception as exc:
            messagebox.showerror("无法打开任务", str(exc), parent=self.root)
            return
        self.session_id = session_id
        self._flow_session_ids[self.flow] = session_id
        self._render_active_session(session)

    def _new_task(self) -> None:
        if self._busy:
            return
        self.session_id = None
        self._flow_session_ids[self.flow] = None
        if self.flow == FlowKind.DEVELOPMENT and not self._applications_injected:
            self.application = self._build_current_development_application()
        elif self.flow == FlowKind.INCIDENT and not self._incident_application_injected:
            self.incident_application = self._build_current_incident_application()
        self.sessions_list.selection_clear(0, "end")
        self.task_title_var.set("新开发任务" if self.flow == FlowKind.DEVELOPMENT else "新异常诊断")
        self._hide_approval()
        self._refresh_project_options()
        self._render_welcome()
        self._sync_controls()
        self.prompt_input.focus_set()

    def _browse_workspace(self) -> None:
        if self._busy:
            return
        initial = self.workspace_var.get().strip()
        if not Path(initial).is_dir():
            initial = str(Path.cwd())
        selected = filedialog.askdirectory(
            title="选择项目目录", initialdir=initial, mustexist=True, parent=self.root
        )
        if selected:
            self.workspace_var.set(str(Path(selected).resolve()))

    def _open_system_settings(self, section: str = "model") -> None:
        if self._busy:
            return
        if self._settings_dialog is not None and self._settings_dialog.window.winfo_exists():
            selected_tab = {
                "database": self._settings_dialog.database_tab,
                "knowledge": self._settings_dialog.knowledge_tab,
            }.get(section, self._settings_dialog.model_tab)
            self._settings_dialog.notebook.select(selected_tab)
            self._settings_dialog.window.lift()
            self._settings_dialog.window.focus_force()
            return
        self._settings_dialog = SystemSettingsDialog(
            self.root,
            self.setup_service,
            self.sqlserver_service,
            initial_section=section,
            on_model_saved=self._apply_model_configuration,
            on_database_saved=self._apply_sqlserver_configuration,
            on_knowledge_changed=self._refresh_project_options,
        )

    def _apply_sqlserver_configuration(self, state: SQLServerConfigState) -> None:
        if not state.configured or state.config is None:
            self.status_var.set("SQL Server 连接尚未配置完成。")
            return
        active_development = self._flow_session_ids[FlowKind.DEVELOPMENT]
        active_incident = self._flow_session_ids[FlowKind.INCIDENT]
        if not self._applications_injected and not active_development:
            self.application = self._build_current_development_application()
        if not self._incident_application_injected and not active_incident:
            self.incident_application = self._build_current_incident_application()
        self.status_var.set(
            "SQL Server 配置已保存；当前任务保持原连接，新任务使用新连接。"
            if active_development or active_incident
            else "SQL Server 配置已保存，两套流程立即共享。"
        )
        self._sync_controls()

    def _open_log_directory(self) -> None:
        log_path = getattr(self._active_application(), "log_path", None)
        if log_path is None:
            messagebox.showinfo(
                "本地日志",
                "当前应用实例没有配置日志文件。",
                parent=self.root,
            )
            return
        directory = Path(log_path).expanduser().resolve().parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(directory)  # type: ignore[attr-defined]
            else:
                webbrowser.open(directory.as_uri())
        except OSError as exc:
            messagebox.showerror(
                "无法打开日志目录",
                f"{directory}\n\n{exc}",
                parent=self.root,
            )

    def _apply_model_configuration(self, state: ModelSetupState) -> None:
        """Rebuild runtimes so the newly saved provider values apply immediately."""

        if not state.ready:
            self.status_var.set("模型配置尚未就绪。")
            return
        if not self._applications_injected:
            if not self._flow_session_ids[FlowKind.DEVELOPMENT]:
                self.application = self._build_current_development_application()
            if not self._flow_session_ids[FlowKind.INCIDENT]:
                self.incident_application = self._build_current_incident_application()
            self._incident_application_injected = False
            self._load_recent_sessions(select_current=bool(self.session_id))
        self.status_var.set("模型配置已保存并立即生效。")
        self._sync_controls()

    def _incident_application_for_database(self, reference: str | None) -> IncidentApplication:
        if self._incident_application_injected:
            if self.incident_application is None:
                raise RuntimeError("Incident application is not configured.")
            return self.incident_application
        if self.incident_application is not None and (
            reference == self._active_incident_database_reference
        ):
            return self.incident_application
        if reference and not reference.startswith("sqlserver://"):
            self.incident_application = build_incident_application(sqlite_path=reference)
            self._active_incident_database_reference = reference
            return self.incident_application
        state = self.sqlserver_service.inspect()
        if reference and (state.config is None or state.config.reference != reference):
            raise RuntimeError(
                "此历史异常使用的 SQL Server 连接已被更换。请恢复原连接，或新建异常诊断。"
            )
        self.incident_application = self._build_current_incident_application()
        return self.incident_application

    def _build_current_incident_application(self) -> IncidentApplication:
        reader = self.sqlserver_service.reader()
        reference = reader.reference if reader is not None else None
        self._active_incident_database_reference = reference
        return build_incident_application(
            database=reader,
            database_reference=reference,
        )

    def _build_current_development_application(self) -> AgentApplication:
        reader = self.sqlserver_service.reader()
        reference = reader.reference if reader is not None else None
        self._active_development_database_reference = reference
        return build_application(
            database=reader,
            database_reference=reference,
        )

    def _send_message(self) -> None:
        if self._busy:
            return
        message = self.prompt_input.get("1.0", "end-1c").strip()
        if not message:
            self.status_var.set("请先输入任务内容。")
            return

        if self.session_id is None:
            project = self.project_var.get().strip()
            if not project:
                self.status_var.set("请先选择项目；可在系统配置中添加项目。")
                return
            workspace = self.workspace_var.get().strip()
            if not workspace:
                self.status_var.set("请先选择项目目录。")
                return
            path = Path(workspace).expanduser()
            if not path.is_dir():
                self.status_var.set("项目目录不存在，请重新选择。")
                return
            if self.flow == FlowKind.DEVELOPMENT:

                def operation() -> AgentOutcome | IncidentOutcome:
                    return self.application.start(path, message, project)

            else:
                page_hint = self.page_hint_var.get().strip() or None
                database_reference = self._active_incident_database_reference

                def operation() -> AgentOutcome | IncidentOutcome:
                    application = self._incident_application_for_database(database_reference)
                    return application.start(path, message, page_hint, project=project)

        else:
            session_id = self.session_id
            if self.flow == FlowKind.DEVELOPMENT:

                def operation() -> AgentOutcome | IncidentOutcome:
                    return self.application.send(session_id, message)

            else:
                current_application = self._active_application()
                if not isinstance(current_application, IncidentApplication) and not (
                    self._incident_application_injected
                ):
                    raise RuntimeError("Incident application is not configured.")
                current_session = current_application.get_session(session_id)
                database_reference = (
                    current_session.database_reference or self._active_incident_database_reference
                )

                def operation() -> AgentOutcome | IncidentOutcome:
                    application = self._incident_application_for_database(database_reference)
                    return application.send(session_id, message)

        self.prompt_input.delete("1.0", "end")
        self._append_optimistic_user_message(message)
        self._run_in_background(operation, "Claude Code 正在分析")

    def _append_optimistic_user_message(self, message: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", "你\n", "user_name")
        self.transcript.insert("end", f"{message}\n\n", "message")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _approve(self) -> None:
        if (
            self.flow != FlowKind.DEVELOPMENT
            or self._busy
            or not self.session_id
            or not self._approval_can_execute
        ):
            return
        session_id = self.session_id
        self._run_in_background(
            lambda: self.application.approve(session_id), "Claude Code 正在执行已批准的操作"
        )

    def _reject(self) -> None:
        if self.flow != FlowKind.DEVELOPMENT or self._busy or not self.session_id:
            return
        reason = simpledialog.askstring(
            "拒绝或调整方案",
            "请说明希望调整的内容（可选）：",
            parent=self.root,
        )
        if reason is None:
            return
        session_id = self.session_id
        self._run_in_background(
            lambda: self.application.reject(session_id, reason),
            "Claude Code 正在按只读范围继续",
        )

    def _resume_recovery(self) -> None:
        if self._busy or not self.session_id:
            return
        session_id = self.session_id
        application = self._active_application()
        self._run_in_background(
            lambda: application.resume(
                session_id,
                RecoveryAction.READ_ONLY_INSPECT,
            ),
            "Claude Code 正在只读检查恢复现场",
        )

    def _replan_recovery(self) -> None:
        if self._busy or not self.session_id:
            return
        session_id = self.session_id
        application = self._active_application()
        self._run_in_background(
            lambda: application.resume(session_id, RecoveryAction.REPLAN),
            "Claude Code 正在重新调查并制定方案",
        )

    def _cancel_recovery(self) -> None:
        if self._busy or not self.session_id:
            return
        if not messagebox.askyesno(
            "取消任务",
            (
                "确认取消此任务？现有工作区内容不会被自动回滚。"
                if self.flow == FlowKind.DEVELOPMENT
                else "确认取消此异常诊断？已读取的数据不会写回数据库。"
            ),
            parent=self.root,
        ):
            return
        session_id = self.session_id
        application = self._active_application()
        self._run_in_background(
            lambda: application.cancel(session_id),
            "正在取消任务",
        )

    def _run_in_background(
        self,
        operation: Callable[[], AgentOutcome | IncidentOutcome],
        label: str,
    ) -> None:
        self._set_busy(True, label)

        def worker() -> None:
            try:
                self._result_queue.put(("success", operation()))
            except Exception as exc:
                self._result_queue.put(("error", exc))

        threading.Thread(target=worker, name="agent-turn", daemon=True).start()

    def _drain_results(self) -> None:
        try:
            while True:
                kind, payload = self._result_queue.get_nowait()
                self._set_busy(False)
                if kind == "success":
                    outcome = payload
                    if self.flow == FlowKind.DEVELOPMENT and not isinstance(outcome, AgentOutcome):
                        raise TypeError("Development operation returned an invalid outcome.")
                    if self.flow == FlowKind.INCIDENT and not isinstance(outcome, IncidentOutcome):
                        raise TypeError("Incident operation returned an invalid outcome.")
                    self.session_id = outcome.session_id
                    self._flow_session_ids[self.flow] = outcome.session_id
                    session = self._active_application().get_session(outcome.session_id)
                    self._render_active_session(session)
                    self._load_recent_sessions(select_current=True)
                else:
                    self.status_var.set(f"操作失败：{payload}")
                    messagebox.showerror("操作失败", str(payload), parent=self.root)
                    if self.session_id:
                        try:
                            session = self._active_application().get_session(self.session_id)
                            self._render_active_session(session)
                        except Exception:
                            pass
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._drain_results)

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = busy
        self._busy_label = label
        self._busy_tick = 0
        self._sync_controls()
        if busy:
            self._animate_busy_status()
        elif self.session_id:
            try:
                self._set_status(self._active_application().get_session(self.session_id).status)
            except Exception:
                self._set_status(None)
        else:
            self._set_status(None)

    def _animate_busy_status(self) -> None:
        if not self._busy:
            return
        dots = "." * (self._busy_tick % 4)
        self._busy_tick += 1
        self.status_var.set(f"{self._busy_label}{dots}")
        self.status_badge.configure(
            text="处理中",
            fg=COLORS["accent"],
            bg=COLORS["accent_soft"],
            highlightbackground=COLORS["accent_border"],
        )
        self.root.after(450, self._animate_busy_status)

    def _set_status(self, status: AgentStatus | IncidentStatus | None) -> None:
        self._current_status = status
        if isinstance(status, IncidentStatus):
            label, color = INCIDENT_STATUS_PRESENTATION[status]
        else:
            label, color = STATUS_PRESENTATION[status]
        surface, border = STATUS_SURFACES[status]
        self.status_badge.configure(
            text=label,
            fg=color,
            bg=surface,
            highlightbackground=border,
        )
        if not self._busy and status is None:
            self.status_var.set("就绪")

    def _sync_controls(self) -> None:
        """Keep controls consistent with the durable task state and current worker."""

        can_start_new = not self._busy
        can_choose_workspace = not self._busy and self.session_id is None
        can_send = not self._busy and self._current_status not in {
            AgentStatus.COMPLETED,
            IncidentStatus.COMPLETED,
        }
        can_decide = (
            self.flow == FlowKind.DEVELOPMENT
            and not self._busy
            and self._current_status == AgentStatus.APPROVAL_REQUIRED
        )
        self.new_task_button.configure(state="normal" if can_start_new else "disabled")
        self.workspace_entry.configure(state="normal" if can_choose_workspace else "disabled")
        self.browse_button.configure(state="normal" if can_choose_workspace else "disabled")
        self.project_combo.configure(state="readonly" if can_choose_workspace else "disabled")
        recovery_state = False
        if self.session_id:
            try:
                recovery_state = self._active_application().get_session(
                    self.session_id
                ).task_state in {
                    TaskState.PAUSED,
                    TaskState.RECOVERY_REQUIRED,
                }
            except Exception:
                recovery_state = False
        incident_input_state = (
            "normal" if can_choose_workspace and self.flow == FlowKind.INCIDENT else "disabled"
        )
        self.page_hint_entry.configure(state=incident_input_state)
        self.prompt_input.configure(state="normal" if can_send else "disabled")
        self.send_button.configure(state="normal" if can_send else "disabled")
        self.approve_button.configure(
            state="normal" if can_decide and self._approval_can_execute else "disabled"
        )
        self.reject_button.configure(state="normal" if can_decide else "disabled")
        if recovery_state and not self._busy:
            self.reject_button.configure(state="normal")
            self.approve_button.configure(state="normal")
            self.recovery_replan_button.configure(state="normal")
        else:
            self.recovery_replan_button.configure(state="disabled")
        self.sessions_list.configure(state="normal" if not self._busy else "disabled")
        self.model_config_button.configure(state="normal" if not self._busy else "disabled")
        self.development_flow_button.set_enabled(not self._busy)
        self.incident_flow_button.set_enabled(
            not self._busy and self.incident_application is not None
        )

    def _show_approval(self, session: AgentSession) -> None:
        approval = session.pending_approval
        if approval is None and session.task_state in {
            TaskState.PAUSED,
            TaskState.RECOVERY_REQUIRED,
        }:
            recovery = next(
                (
                    artifact
                    for artifact in reversed(session.artifacts)
                    if artifact.type.value == "recovery_report"
                ),
                None,
            )
            self._approval_can_execute = True
            self.approval_title.configure(text="恢复任务 · 不会自动重放写操作")
            self.approval_text.configure(state="normal")
            self.approval_text.delete("1.0", "end")
            self.approval_text.insert(
                "1.0",
                "检测到中断任务。请选择：只读检查当前现场、放弃旧方案后重新规划，"
                "或取消任务。\n\n"
                + (
                    f"恢复报告：{recovery.relative_path}"
                    if recovery is not None
                    else "恢复报告未能生成，请先选择只读检查。"
                ),
            )
            self.approval_text.configure(state="disabled")
            self.reject_button.configure(text="取消任务", command=self._cancel_recovery)
            self.approve_button.configure(text="只读检查", command=self._resume_recovery)
            self.recovery_replan_button.configure(
                text="重新规划", command=self._replan_recovery
            )
            self.recovery_replan_button.grid(row=0, column=2, padx=4)
            self.approve_button.grid_configure(column=3)
            self.approval_frame.grid(row=2, column=0, sticky="ew", padx=48, pady=(10, 0))
            return
        if approval is None:
            self._hide_approval()
            return
        self.reject_button.configure(text="拒绝或调整", command=self._reject)
        self.approve_button.configure(command=self._approve)
        self.approve_button.grid_configure(column=2)
        self.recovery_replan_button.grid_remove()
        is_modify = approval.scope == ApprovalScope.MODIFY
        self._approval_can_execute = not (is_modify and approval.proposal is None)
        self.approval_title.configure(
            text=(
                "旧审批缺少修改方案 · 请拒绝并说明需要重新生成方案"
                if is_modify and approval.proposal is None
                else "修改方案 · 确认后才会编辑文件"
                if is_modify
                else "验证方案 · 确认后才会运行命令"
            )
        )
        self.approve_button.configure(
            text=(
                "需要重新生成"
                if is_modify and approval.proposal is None
                else "批准此方案"
                if is_modify
                else "批准并验证"
            )
        )
        self.approval_text.configure(state="normal")
        self.approval_text.delete("1.0", "end")
        self.approval_text.insert("1.0", format_approval_details(approval))
        self.approval_text.yview_moveto(0.0)
        self.approval_text.configure(state="disabled")
        self.approval_frame.grid(row=2, column=0, sticky="ew", padx=48, pady=(10, 0))

    def _show_incident_recovery(self, session: IncidentSession) -> None:
        self._approval_can_execute = True
        self.approval_title.configure(text="恢复异常诊断 · 只读查询不会自动重放")
        self.approval_text.configure(state="normal")
        self.approval_text.delete("1.0", "end")
        self.approval_text.insert(
            "1.0",
            (session.last_decision.message if session.last_decision else "异常诊断已暂停。")
            + "\n\n继续后 Agent 会重新核对代码和数据库 schema，并自行形成、执行最小只读 SQL。",
        )
        self.approval_text.configure(state="disabled")
        self.reject_button.configure(text="取消诊断", command=self._cancel_recovery)
        self.approve_button.configure(text="继续只读诊断", command=self._resume_recovery)
        self.recovery_replan_button.configure(text="重新调查", command=self._replan_recovery)
        self.recovery_replan_button.grid(row=0, column=2, padx=4)
        self.approve_button.grid_configure(column=3)
        self.approval_frame.grid(row=2, column=0, sticky="ew", padx=48, pady=(10, 0))

    def _hide_approval(self) -> None:
        self._approval_can_execute = True
        self.recovery_replan_button.grid_remove()
        self.approve_button.grid_configure(column=2)
        self.approval_frame.grid_remove()

    def _on_return(self, event: tk.Event[tk.Text]) -> str | None:
        if event.state & 0x0001:  # Shift+Enter keeps the normal newline behavior.
            return None
        self._send_message()
        return "break"

    def _on_control_return(self, _event: tk.Event[tk.Text]) -> str:
        self._send_message()
        return "break"

    def _on_close(self) -> None:
        if self._busy:
            messagebox.showwarning(
                "任务仍在运行",
                "Claude Code 仍在处理当前任务。请等待本轮结束后再关闭客户端。",
                parent=self.root,
            )
            return
        self.root.destroy()


def _enable_windows_dpi_awareness() -> None:
    if not hasattr(ctypes, "windll"):
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _acquire_single_instance() -> bool:
    """Prevent two desktop windows from writing the same session store concurrently."""

    global _instance_mutex
    if not hasattr(ctypes, "windll"):
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, False, _INSTANCE_MUTEX_NAME)
    if not handle:
        return True
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _instance_mutex = int(handle)
    return True


def _release_single_instance() -> None:
    global _instance_mutex
    if _instance_mutex is None or not hasattr(ctypes, "windll"):
        return
    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_instance_mutex))
    _instance_mutex = None


def main() -> None:
    """Start the native desktop client."""

    _enable_windows_dpi_awareness()
    if not _acquire_single_instance():
        duplicate_root = tk.Tk()
        duplicate_root.withdraw()
        messagebox.showinfo(
            "AutoCoding Engineer",
            "客户端已经在运行，请切换到已有窗口。",
            parent=duplicate_root,
        )
        duplicate_root.destroy()
        return
    root = tk.Tk()
    root.withdraw()
    setup_service = ClaudeModelSetupService()
    sqlserver_service = SQLServerConnectionService()
    client: DesktopClient | None = None

    def launch_client(_state: ModelSetupState | None = None) -> None:
        nonlocal client
        if not root.winfo_exists() or client is not None:
            return
        root.deiconify()
        root.state("normal")
        root.lift()
        root.focus_force()
        client = DesktopClient(
            root,
            setup_service=setup_service,
            sqlserver_service=sqlserver_service,
        )

    try:
        setup_state = setup_service.inspect()
        if setup_state.ready:
            launch_client(setup_state)
        else:
            SystemSettingsDialog(
                root,
                setup_service,
                sqlserver_service,
                initial_section="model",
                required_model_setup=True,
                on_model_saved=launch_client,
            )
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("客户端启动失败", str(exc), parent=root)
        root.destroy()
        _release_single_instance()
        raise
    try:
        root.mainloop()
    finally:
        _release_single_instance()


if __name__ == "__main__":
    main()
