"""One desktop settings window shared by every workflow."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from uuid import uuid4

from autocoding_agent.datasource_models import DataSourceDefinition
from autocoding_agent.datasources import DataSourceService
from autocoding_agent.embedding_setup import (
    SUPPORTED_EMBEDDING_DIMENSIONS,
    EmbeddingConnectionConfig,
    EmbeddingSetupError,
    EmbeddingSetupService,
    EmbeddingSetupState,
)
from autocoding_agent.interfaces.datasource_ui import edit_project_sources, edit_source
from autocoding_agent.interfaces.oracle_settings_ui import OracleSettingsDialog
from autocoding_agent.model_setup import (
    ClaudeModelSetupService,
    ModelSetupError,
    ModelSetupState,
)
from autocoding_agent.sqlserver_config import (
    SQLServerAuthentication,
    SQLServerConfigState,
    SQLServerConnectionConfig,
)
from autocoding_agent.sqlserver_service import (
    SQLServerConnectionService,
    build_connection_config,
)
from autocoding_agent.workspace_config import (
    WorkspaceConfigService,
    WorkspaceConfigState,
    WorkspaceProject,
)
from autocoding_agent.workspace_knowledge import (
    KnowledgeDomain,
    MarkdownKnowledgeService,
    WorkspaceKnowledgeError,
)

WINDOW = "#EEF3FA"
CARD = "#F9FBFE"
PANEL = "#F1F5F9"
BORDER = "#DCE5F0"
TEXT = "#111827"
MUTED = "#475569"
ACCENT = "#2563EB"
SUCCESS = "#15803D"
DANGER = "#DC2626"


class SystemSettingsDialog:
    """Central model and database configuration used by development and incidents."""

    def __init__(
        self,
        parent: tk.Misc,
        model_service: ClaudeModelSetupService,
        database_service: SQLServerConnectionService,
        *,
        initial_section: str = "model",
        embedding_service: EmbeddingSetupService | None = None,
        knowledge_service: MarkdownKnowledgeService | None = None,
        workspace_service: WorkspaceConfigService | None = None,
        required_model_setup: bool = False,
        on_model_saved: Callable[[ModelSetupState], None] | None = None,
        on_embedding_saved: Callable[[EmbeddingSetupState], None] | None = None,
        on_database_saved: Callable[[SQLServerConfigState], None] | None = None,
        on_knowledge_changed: Callable[[], None] | None = None,
        on_workspace_saved: Callable[[WorkspaceConfigState], None] | None = None,
        on_datasources_changed: Callable[[], None] | None = None,
    ) -> None:
        self.parent = parent
        self.model_service = model_service
        self.database_service = database_service
        self.datasource_service = DataSourceService(database_service.settings)
        self._editing_source_id = None
        self.embedding_service = embedding_service or EmbeddingSetupService()
        self.knowledge_service = knowledge_service or MarkdownKnowledgeService()
        self.workspace_service = workspace_service or WorkspaceConfigService(
            knowledge=self.knowledge_service,
        )
        self.workspace_service.knowledge = self.knowledge_service
        self.projects = self.workspace_service.list_projects()
        self._editing_project_id: str | None = self.projects[0].id if self.projects else None
        self.on_model_saved = on_model_saved
        self.on_embedding_saved = on_embedding_saved
        self.on_database_saved = on_database_saved
        self.on_knowledge_changed = on_knowledge_changed
        self.on_workspace_saved = on_workspace_saved
        self.on_datasources_changed = on_datasources_changed
        self.required_model_setup = required_model_setup
        self.model_state = model_service.inspect()
        self.embedding_state = self.embedding_service.inspect()
        self.database_state = database_service.inspect()
        self.workspace_state = self.workspace_service.inspect()
        self._test_results: queue.Queue[tuple[str, object]] = queue.Queue()
        self._embedding_test_results: queue.Queue[tuple[str, object]] = queue.Queue()
        self._testing_database = False
        self._testing_embedding = False

        self.window = tk.Toplevel(parent)
        self.window.title("系统配置 · AutoCoding Engineer")
        self.window.configure(bg=WINDOW)
        width, height, geometry = self._geometry(980, 720)
        self.window.geometry(geometry)
        self.window.minsize(min(760, width), min(580, height))
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        if parent.winfo_viewable():
            self.window.transient(parent)

        self._build()
        self._load_model_state(self.model_state)
        self._load_embedding_state(self.embedding_state)
        self._load_database_state(self.database_state)
        self._load_workspace_state(self.workspace_state)
        selected_tab = {
            "database": self.database_tab,
            "embedding": self.embedding_tab,
            "workspace": self.workspace_tab,
            "knowledge": self.knowledge_tab,
        }.get(initial_section, self.model_tab)
        self.notebook.select(selected_tab)
        self._sync_footer_actions()
        self.window.grab_set()
        self.window.focus_force()

    def _geometry(self, width: int, height: int) -> tuple[int, int, str]:
        screen_width = self.parent.winfo_screenwidth()
        screen_height = self.parent.winfo_screenheight()
        width = min(width, max(760, screen_width - 40))
        height = min(height, max(580, screen_height - 70))
        left = max(0, (screen_width - width) // 2)
        top = max(0, (screen_height - height) // 2)
        return width, height, f"{width}x{height}+{left}+{top}"

    def _build(self) -> None:
        self.window.grid_columnconfigure(0, weight=1)
        self.window.grid_rowconfigure(1, weight=1)
        header = tk.Frame(self.window, bg=WINDOW)
        header.grid(row=0, column=0, sticky="ew", padx=32, pady=(22, 10))
        tk.Label(
            header,
            text="系统配置",
            font=("Microsoft YaHei UI", 18, "bold"),
            fg=TEXT,
            bg=WINDOW,
        ).pack(anchor="w")
        tk.Label(
            header,
            text="生成模型、Embedding、数据库、项目路径和 Markdown 知识在这里统一管理。",
            font=("Microsoft YaHei UI", 9),
            fg=MUTED,
            bg=WINDOW,
        ).pack(anchor="w", pady=(3, 0))

        style = ttk.Style(self.window)
        style.configure("Settings.TNotebook", background=WINDOW, borderwidth=0)
        style.configure(
            "Settings.TNotebook.Tab",
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(20, 10),
        )
        style.map(
            "Settings.TNotebook.Tab",
            background=[("selected", "#FFFFFF"), ("active", "#EAF2FF")],
            foreground=[("selected", ACCENT), ("!selected", MUTED)],
        )
        self.notebook = ttk.Notebook(
            self.window,
            style="Settings.TNotebook",
        )
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=30, pady=(0, 12))
        self.model_tab = tk.Frame(self.notebook, bg=CARD)
        self.embedding_tab = tk.Frame(self.notebook, bg=CARD)
        self.database_tab = tk.Frame(self.notebook, bg=CARD)
        self.workspace_tab = tk.Frame(self.notebook, bg=CARD)
        self.knowledge_tab = tk.Frame(self.notebook, bg=CARD)
        self.notebook.add(self.model_tab, text="模型与 Claude Code")
        self.notebook.add(self.embedding_tab, text="Embedding")
        self.notebook.add(self.database_tab, text="数据库")
        self.notebook.add(self.workspace_tab, text="项目维护")
        self.notebook.add(self.knowledge_tab, text="MD 能力配置")
        self._build_model_tab()
        self._build_embedding_tab()
        self._build_database_tab()
        self._build_source_selector()
        self._build_workspace_tab()
        self._build_knowledge_tab()

        footer = tk.Frame(self.window, bg=WINDOW)
        footer.grid(row=2, column=0, sticky="ew", padx=30, pady=(0, 18))
        self.close_button = self._button(footer, "关闭", self._close, PANEL, TEXT)
        self.model_save_button = self._button(
            footer, "保存模型配置", self._save_model, ACCENT, "#FFFFFF"
        )
        self.embedding_test_button = self._button(
            footer, "测试连接", self._test_embedding, PANEL, TEXT
        )
        self.embedding_save_button = self._button(
            footer, "保存 Embedding 配置", self._save_embedding, ACCENT, "#FFFFFF"
        )
        self.database_test_button = self._button(
            footer, "测试连接", self._test_database, PANEL, TEXT
        )
        self.database_save_button = self._button(
            footer, "保存数据库配置", self._save_database, ACCENT, "#FFFFFF"
        )
        self.workspace_save_button = self._button(
            footer, "保存项目", self._save_workspace, ACCENT, "#FFFFFF"
        )
        self.knowledge_save_button = self._button(
            footer, "保存 Markdown", self._save_knowledge, ACCENT, "#FFFFFF"
        )
        self.notebook.bind("<<NotebookTabChanged>>", self._sync_footer_actions)
        self._sync_footer_actions()

    def _build_model_tab(self) -> None:
        tab = self.model_tab
        tab.grid_columnconfigure(0, weight=1)
        self.model_command_var = tk.StringVar()
        self.model_endpoint_var = tk.StringVar()
        self.model_name_var = tk.StringVar()
        self.model_key_var = tk.StringVar()
        self.model_status_var = tk.StringVar()
        self.model_key_hint_var = tk.StringVar()

        self.model_status_label = self._status(tab, self.model_status_var, 0)
        self._field_label(tab, "Claude Code 程序", 1)
        command_row = tk.Frame(tab, bg=CARD)
        command_row.grid(row=2, column=0, sticky="ew", padx=26)
        command_row.grid_columnconfigure(0, weight=1)
        self.model_command_entry = self._entry(command_row, self.model_command_var)
        self.model_command_entry.grid(row=0, column=0, sticky="ew", ipady=5)
        self.model_detect_button = self._button(
            command_row, "自动检测", self._detect_model, PANEL, TEXT
        )
        self.model_detect_button.grid(row=0, column=1, padx=(8, 0))
        self.model_browse_button = self._button(
            command_row, "浏览…", self._browse_model, PANEL, TEXT
        )
        self.model_browse_button.grid(row=0, column=2, padx=(8, 0))

        self._field_label(tab, "API 地址", 3)
        self.model_endpoint_entry = self._entry(tab, self.model_endpoint_var)
        self.model_endpoint_entry.grid(row=4, column=0, sticky="ew", padx=26, ipady=5)
        self._field_label(tab, "模型名称", 5)
        self.model_name_entry = self._entry(tab, self.model_name_var)
        self.model_name_entry.grid(row=6, column=0, sticky="ew", padx=26, ipady=5)
        self._field_label(tab, "API Key", 7)
        self.model_key_entry = self._entry(tab, self.model_key_var, show="●")
        self.model_key_entry.grid(row=8, column=0, sticky="ew", padx=26, ipady=5)
        tk.Label(
            tab,
            textvariable=self.model_key_hint_var,
            font=("Microsoft YaHei UI", 8),
            fg=MUTED,
            bg=CARD,
            anchor="w",
        ).grid(row=9, column=0, sticky="ew", padx=26, pady=(4, 8))
        tk.Label(
            tab,
            text="模型密钥保存到 Windows 当前用户环境，不写入项目或日志。",
            font=("Microsoft YaHei UI", 8),
            fg=MUTED,
            bg=CARD,
            anchor="w",
        ).grid(row=10, column=0, sticky="ew", padx=26, pady=(0, 12))

    def _build_embedding_tab(self) -> None:
        tab = self.embedding_tab
        tab.grid_columnconfigure(0, weight=1)
        self.embedding_endpoint_var = tk.StringVar()
        self.embedding_model_var = tk.StringVar()
        self.embedding_dimension_var = tk.StringVar()
        self.embedding_key_var = tk.StringVar()
        self.embedding_status_var = tk.StringVar()
        self.embedding_key_hint_var = tk.StringVar()

        self.embedding_status_label = self._status(tab, self.embedding_status_var, 0)
        self._field_label(tab, "API 地址", 1)
        self.embedding_endpoint_entry = self._entry(tab, self.embedding_endpoint_var)
        self.embedding_endpoint_entry.grid(
            row=2, column=0, sticky="ew", padx=26, ipady=5
        )
        self._field_label(tab, "Embedding 模型", 3)
        self.embedding_model_entry = self._entry(tab, self.embedding_model_var)
        self.embedding_model_entry.grid(row=4, column=0, sticky="ew", padx=26, ipady=5)
        self._field_label(tab, "输出维度", 5)
        self.embedding_dimension_combo = ttk.Combobox(
            tab,
            textvariable=self.embedding_dimension_var,
            values=[str(value) for value in SUPPORTED_EMBEDDING_DIMENSIONS],
            state="normal",
            font=("Microsoft YaHei UI", 9),
        )
        self.embedding_dimension_combo.grid(
            row=6, column=0, sticky="ew", padx=26, ipady=4
        )
        self._field_label(tab, "API Key", 7)
        self.embedding_key_entry = self._entry(tab, self.embedding_key_var, show="●")
        self.embedding_key_entry.grid(row=8, column=0, sticky="ew", padx=26, ipady=5)
        tk.Label(
            tab,
            textvariable=self.embedding_key_hint_var,
            font=("Microsoft YaHei UI", 8),
            fg=MUTED,
            bg=CARD,
            anchor="w",
        ).grid(row=9, column=0, sticky="ew", padx=26, pady=(4, 6))
        tk.Label(
            tab,
            text=(
                "默认使用 voyage-code-4。API Key 保存到 Windows 凭据管理器，不写入项目或日志。\n"
                "更换 API 地址、模型或维度会创建独立索引；已有 Markdown 需要在知识库管理页"
                "手动重建。建立索引会把所选 Chunk、检索会把查询文本发送到该 API。"
            ),
            justify="left",
            font=("Microsoft YaHei UI", 8),
            fg=MUTED,
            bg=CARD,
            anchor="w",
            wraplength=850,
        ).grid(row=10, column=0, sticky="ew", padx=26, pady=(0, 12))

    def _build_database_tab(self) -> None:
        tab = self.database_tab
        tab.grid_columnconfigure(0, weight=1)
        config = self.database_state.config
        drivers = self.database_service.drivers()
        if config and config.driver not in drivers:
            drivers.insert(0, config.driver)
        self.database_drivers = drivers
        self.db_server_var = tk.StringVar(value=config.server if config else "")
        self.db_port_var = tk.StringVar(value=str(config.port if config else 1433))
        self.db_name_var = tk.StringVar(value=config.database if config else "")
        self.db_driver_var = tk.StringVar(
            value=config.driver if config else drivers[0] if drivers else ""
        )
        self.db_auth_var = tk.StringVar(
            value=(
                config.authentication.value
                if config
                else SQLServerAuthentication.WINDOWS.value
            )
        )
        self.db_username_var = tk.StringVar(value=(config.username or "") if config else "")
        self.db_password_var = tk.StringVar()
        self.db_encrypt_var = tk.BooleanVar(value=config.encrypt if config else True)
        self.db_trust_var = tk.BooleanVar(
            value=config.trust_server_certificate if config else False
        )
        self.database_status_var = tk.StringVar()
        self.database_password_hint_var = tk.StringVar()

        self.database_status_label = self._status(tab, self.database_status_var, 0)
        server_row = tk.Frame(tab, bg=CARD)
        server_row.grid(row=1, column=0, sticky="ew", padx=26, pady=(4, 0))
        server_row.grid_columnconfigure(0, weight=1)
        self._inline_label(server_row, "服务器", 0)
        self._inline_label(server_row, "端口", 1)
        self.db_server_entry = self._entry(server_row, self.db_server_var)
        self.db_server_entry.grid(row=1, column=0, sticky="ew", padx=(0, 10), ipady=5)
        self.db_port_entry = self._entry(server_row, self.db_port_var, width=9)
        self.db_port_entry.grid(row=1, column=1, sticky="ew", ipady=5)
        self._field_label(tab, "数据库", 2)
        self.db_name_entry = self._entry(tab, self.db_name_var)
        self.db_name_entry.grid(row=3, column=0, sticky="ew", padx=26, ipady=5)
        self._field_label(tab, "ODBC 驱动", 4)
        self.db_driver_combo = ttk.Combobox(
            tab,
            textvariable=self.db_driver_var,
            values=drivers,
            state="readonly" if drivers else "disabled",
            font=("Microsoft YaHei UI", 9),
        )
        self.db_driver_combo.grid(row=5, column=0, sticky="ew", padx=26, ipady=4)

        auth_row = tk.Frame(tab, bg=CARD)
        auth_row.grid(row=6, column=0, sticky="w", padx=26, pady=(6, 2))
        tk.Label(
            auth_row,
            text="认证方式",
            font=("Microsoft YaHei UI", 8, "bold"),
            fg=TEXT,
            bg=CARD,
        ).pack(side="left", padx=(0, 12))
        self.db_auth_buttons: list[tk.Radiobutton] = []
        for text, value in (
            ("Windows 集成认证", SQLServerAuthentication.WINDOWS.value),
            ("SQL Server 用户名密码", SQLServerAuthentication.SQL_PASSWORD.value),
        ):
            button = tk.Radiobutton(
                auth_row,
                text=text,
                variable=self.db_auth_var,
                value=value,
                command=self._sync_database_auth,
                font=("Microsoft YaHei UI", 9),
                fg=TEXT,
                bg=CARD,
                activebackground=CARD,
                selectcolor=CARD,
            )
            button.pack(side="left", padx=(0, 14))
            self.db_auth_buttons.append(button)

        credentials = tk.Frame(tab, bg=CARD)
        credentials.grid(row=7, column=0, sticky="ew", padx=26)
        credentials.grid_columnconfigure(0, weight=1)
        credentials.grid_columnconfigure(1, weight=1)
        self._inline_label(credentials, "用户名", 0)
        self._inline_label(credentials, "密码", 1)
        self.db_username_entry = self._entry(credentials, self.db_username_var)
        self.db_username_entry.grid(row=1, column=0, sticky="ew", padx=(0, 10), ipady=5)
        self.db_password_entry = self._entry(credentials, self.db_password_var, show="●")
        self.db_password_entry.grid(row=1, column=1, sticky="ew", ipady=5)
        tk.Label(
            credentials,
            textvariable=self.database_password_hint_var,
            font=("Microsoft YaHei UI", 8),
            fg=MUTED,
            bg=CARD,
            anchor="e",
        ).grid(row=2, column=1, sticky="e", pady=(3, 0))

        options = tk.Frame(tab, bg=CARD)
        options.grid(row=8, column=0, sticky="w", padx=26, pady=(4, 4))
        self.db_option_buttons: list[tk.Checkbutton] = []
        for text, variable in (
            ("加密连接", self.db_encrypt_var),
            ("信任服务器证书", self.db_trust_var),
        ):
            button = tk.Checkbutton(
                options,
                text=text,
                variable=variable,
                font=("Microsoft YaHei UI", 9),
                fg=TEXT,
                bg=CARD,
                activebackground=CARD,
                selectcolor=CARD,
            )
            button.pack(side="left", padx=(0, 18))
            self.db_option_buttons.append(button)

    def _build_source_selector(self) -> None:
        # 原连接字段和测试逻辑继续复用，仅增加稳定的数据源选择。
        for child in self.database_tab.grid_slaves():
            child.grid_configure(row=int(child.grid_info()["row"]) + 1)
        self.source_combo = ttk.Combobox(self.database_tab, state="readonly", width=65)
        self.source_combo.grid(row=0, column=0, sticky="ew", padx=26, pady=8)
        self._refresh_sources()
        self.source_combo.bind("<<ComboboxSelected>>", self._select_source)

    def _refresh_sources(self) -> None:
        self._source_choices = list(self.datasource_service.load().sources.values())
        self.source_combo.configure(values=["旧单连接（兼容历史任务）", "新增 SQL Server"] + [
            f"{source.name} / {source.environment} / {source.database_type} "
            f"[{source.datasource_id}]"
            for source in self._source_choices
        ] + ["新增 Oracle 数据源"])
        ids = [None, "new"] + [source.datasource_id for source in self._source_choices]
        self.source_combo.current(ids.index(self._editing_source_id))

    def _select_source(self, _event=None) -> None:
        index = self.source_combo.current()
        selected = (
            self._source_choices[index - 2] if 2 <= index < len(self._source_choices) + 2 else None
        )
        if index == len(self._source_choices) + 2 or (
            selected and selected.database_type == "oracle"
        ):
            # Oracle 使用独立字段表单，返回后恢复 SQL Server 表单，避免串用连接字段。
            self.source_combo.current(0)
            self._select_source()

            def saved():
                self._refresh_sources()
                if self.on_datasources_changed is not None:
                    self.on_datasources_changed()

            self._oracle_dialog = OracleSettingsDialog(
                self.window, self.datasource_service, selected, on_saved=saved,
            )
            return
        self._editing_source_id = (
            None if index == 0 else "new" if index == 1
            else self._source_choices[index - 2].datasource_id
        )
        service = self._selected_connection_service()
        state = service.inspect()
        config = state.config
        # 新增保留当前表单作为模板，但不复制密码，避免凭据意外跨连接复用。
        if config is not None:
            for field, value in {
                "server": config.server, "port": str(config.port), "name": config.database,
                "driver": config.driver, "auth": config.authentication.value,
                "username": config.username or "", "encrypt": config.encrypt,
                "trust": config.trust_server_certificate,
            }.items():
                getattr(self, f"db_{field}_var").set(value)
        self.db_password_var.set("")
        self._load_database_state(state)

    def _selected_connection_service(self):
        if self._editing_source_id is None:
            return self.database_service
        return self.datasource_service.connection(
            "ds-unsaved" if self._editing_source_id == "new" else self._editing_source_id,
        )

    def _edit_project_sources(self) -> None:
        if self._editing_project_id is None:
            messagebox.showinfo(
                "请先保存项目", "新增项目保存后才能绑定数据源。", parent=self.window,
            )
            return
        edit_project_sources(
            self.window, self.datasource_service, self._editing_project_id,
            self.workspace_name_var.get(),
            on_saved=self.on_datasources_changed,
        )

    def _build_workspace_tab(self) -> None:
        tab = self.workspace_tab
        tab.grid_columnconfigure(0, weight=1)
        self.workspace_path_var = tk.StringVar()
        self.workspace_name_var = tk.StringVar()
        self.workspace_choice_var = tk.StringVar()
        self.git_remote_var = tk.StringVar()
        self.git_branch_var = tk.StringVar()
        self.workspace_status_var = tk.StringVar()
        self.workspace_status_label = self._status(tab, self.workspace_status_var, 0)
        project_row = tk.Frame(tab, bg=CARD)
        project_row.grid(row=1, column=0, sticky="ew", padx=26, pady=8)
        self.workspace_combo = ttk.Combobox(
            project_row, textvariable=self.workspace_choice_var, state="readonly",
            values=[p.name for p in self.projects], width=26,
        )
        self.workspace_combo.pack(side="left")
        self.workspace_combo.bind("<<ComboboxSelected>>", self._select_workspace_project)
        self._button(project_row, "新增项目", self._new_workspace_project, PANEL, TEXT).pack(
            side="left", padx=10,
        )
        self._button(project_row, "绑定数据源", self._edit_project_sources, PANEL, TEXT).pack(
            side="left", padx=4,
        )
        self._field_label(tab, "项目名称", 2)
        self._entry(tab, self.workspace_name_var).grid(
            row=3, column=0, sticky="ew", padx=26, ipady=5,
        )
        self._field_label(tab, "项目代码根目录", 4)
        path_row = tk.Frame(tab, bg=CARD)
        path_row.grid(row=5, column=0, sticky="ew", padx=26)
        path_row.grid_columnconfigure(0, weight=1)
        self.workspace_path_entry = self._entry(path_row, self.workspace_path_var)
        self.workspace_path_entry.grid(row=0, column=0, sticky="ew", ipady=5)
        self.workspace_browse_button = self._button(
            path_row, "浏览…", self._browse_workspace, PANEL, TEXT
        )
        self.workspace_browse_button.grid(row=0, column=1, padx=(8, 0))
        tk.Label(
            tab,
            text=(
                "每个项目绑定独立目录和两份知识文件；旧会话仍使用创建时的目录。"
            ),
            font=("Microsoft YaHei UI", 8),
            fg=MUTED,
            bg=CARD,
            anchor="w",
        ).grid(row=6, column=0, sticky="ew", padx=26, pady=(8, 12))
        self._field_label(tab, "Git 远端地址或名称（例如 origin）", 7)
        self._entry(tab, self.git_remote_var).grid(
            row=8, column=0, sticky="ew", padx=26, ipady=5,
        )
        self._field_label(tab, "目标分支", 9)
        self._entry(tab, self.git_branch_var).grid(
            row=10, column=0, sticky="ew", padx=26, ipady=5,
        )

    def _build_knowledge_tab(self) -> None:
        tab = self.knowledge_tab
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(3, weight=1)
        self.knowledge_domain_var = tk.StringVar(value="开发")
        self.knowledge_branch_var = tk.StringVar()
        self.knowledge_path_var = tk.StringVar()
        self.knowledge_status_var = tk.StringVar()
        self._loaded_knowledge: tuple[str, str] | None = None

        self.knowledge_status_label = self._status(tab, self.knowledge_status_var, 0)

        branch_row = tk.Frame(tab, bg=CARD)
        branch_row.grid(row=1, column=0, sticky="ew", padx=26, pady=(2, 8))
        branch_row.grid_columnconfigure(5, weight=1)
        self._inline_row_label(branch_row, "流程", 0)
        self.knowledge_domain_combo = ttk.Combobox(
            branch_row,
            textvariable=self.knowledge_domain_var,
            values=["入口定位", "开发", "异常处理"],
            state="readonly",
            width=10,
            font=("Microsoft YaHei UI", 9),
        )
        self.knowledge_domain_combo.grid(row=0, column=1, padx=(8, 16), ipady=4)
        self.knowledge_domain_combo.bind(
            "<<ComboboxSelected>>", self._on_knowledge_domain_changed
        )
        self._inline_row_label(branch_row, "项目", 2)
        self.knowledge_branch_combo = ttk.Combobox(
            branch_row,
            textvariable=self.knowledge_branch_var,
            state="readonly",
            width=16,
            font=("Microsoft YaHei UI", 9),
        )
        self.knowledge_branch_combo.grid(row=0, column=3, padx=(8, 16), ipady=4)
        self.knowledge_branch_combo.bind(
            "<<ComboboxSelected>>", self._on_knowledge_branch_changed
        )
        path_row = tk.Frame(tab, bg=CARD)
        path_row.grid(row=2, column=0, sticky="ew", padx=26, pady=(0, 6))
        path_row.grid_columnconfigure(1, weight=1)
        self._inline_row_label(path_row, "MD 文档路径", 0)
        self.knowledge_path_entry = self._entry(path_row, self.knowledge_path_var)
        self.knowledge_path_entry.grid(
            row=0, column=1, sticky="ew", padx=(8, 0), ipady=4
        )
        self.knowledge_path_entry.configure(state="readonly", readonlybackground=PANEL)

        editor_frame = tk.Frame(tab, bg=CARD)
        editor_frame.grid(row=3, column=0, sticky="nsew", padx=26, pady=(0, 12))
        editor_frame.grid_columnconfigure(0, weight=1)
        editor_frame.grid_rowconfigure(0, weight=1)
        self.knowledge_editor = tk.Text(
            editor_frame,
            wrap="word",
            undo=True,
            font=("Consolas", 10),
            fg=TEXT,
            bg="#FFFFFF",
            insertbackground=TEXT,
            selectbackground="#BFDBFE",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            padx=10,
            pady=8,
        )
        self.knowledge_editor.grid(row=0, column=0, sticky="nsew")
        knowledge_scroll = ttk.Scrollbar(
            editor_frame, orient="vertical", command=self.knowledge_editor.yview
        )
        knowledge_scroll.grid(row=0, column=1, sticky="ns")
        self.knowledge_editor.configure(yscrollcommand=knowledge_scroll.set)
        self.knowledge_editor.bind("<<Modified>>", self._on_knowledge_modified)
        self._refresh_knowledge()

    def _sync_footer_actions(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        for button in (
            self.close_button,
            self.model_save_button,
            self.embedding_test_button,
            self.embedding_save_button,
            self.database_test_button,
            self.database_save_button,
            self.workspace_save_button,
            self.knowledge_save_button,
        ):
            button.pack_forget()
        self.close_button.pack(side="right")
        if self.notebook.select() == str(self.database_tab):
            self.database_save_button.pack(side="right", padx=(0, 8))
            self.database_test_button.pack(side="right", padx=(0, 8))
        elif self.notebook.select() == str(self.embedding_tab):
            self.embedding_save_button.pack(side="right", padx=(0, 8))
            self.embedding_test_button.pack(side="right", padx=(0, 8))
        elif self.notebook.select() == str(self.workspace_tab):
            self.workspace_save_button.pack(side="right", padx=(0, 8))
        elif self.notebook.select() == str(self.knowledge_tab):
            self.knowledge_save_button.pack(side="right", padx=(0, 8))
        else:
            self.model_save_button.pack(side="right", padx=(0, 8))

    def _load_model_state(self, state: ModelSetupState) -> None:
        installation = state.installation
        self.model_command_var.set(installation.command or self.model_command_var.get())
        self.model_endpoint_var.set(state.endpoint)
        self.model_name_var.set(state.model)
        self.model_key_var.set("")
        if installation.found:
            self._set_model_status(
                f"已检测 Claude Code · {installation.version or '可用'}",
                SUCCESS,
            )
        else:
            self._set_model_status(installation.error or "未检测到 Claude Code。", DANGER)
        self.model_key_hint_var.set(
            "已配置密钥；留空保持不变。" if state.has_api_key else "请输入 API Key。"
        )

    def _load_embedding_state(self, state: EmbeddingSetupState) -> None:
        self.embedding_state = state
        config = state.config or self.embedding_service.defaults()
        self.embedding_endpoint_var.set(config.endpoint)
        self.embedding_model_var.set(config.model)
        self.embedding_dimension_var.set(str(config.output_dimension))
        self.embedding_key_var.set("")
        self.embedding_key_hint_var.set(
            "已配置密钥；留空保持不变。" if state.has_api_key else "请输入 API Key。"
        )
        if state.configured:
            self._set_embedding_status(
                f"已配置 Voyage · {config.model} · {config.output_dimension} 维",
                SUCCESS,
            )
        else:
            self._set_embedding_status(
                "尚未配置 Voyage；知识库继续使用明确标识的模拟 Embedding。",
                MUTED,
            )

    def _load_database_state(self, state: SQLServerConfigState) -> None:
        self.database_state = state
        if not self.database_drivers:
            self._set_database_status("未检测到 SQL Server ODBC 驱动。", DANGER)
        elif state.configured and state.config:
            self._set_database_status(f"已保存 · {state.config.reference}", SUCCESS)
        else:
            self._set_database_status("尚未保存 SQL Server 连接。", MUTED)
        self.database_password_hint_var.set(
            "已保存；留空保持不变" if state.has_password else "不会显示已有密码"
        )
        self._sync_database_auth()

    def _load_workspace_state(self, state: WorkspaceConfigState) -> None:
        self.workspace_state = state
        if isinstance(state.config, WorkspaceProject):
            self._editing_project_id = state.config.id
            self.workspace_name_var.set(state.config.name)
            self.workspace_choice_var.set(state.config.name)
        elif self.projects:
            self._editing_project_id = self.projects[0].id
            self.workspace_name_var.set(self.projects[0].name)
            self.workspace_choice_var.set(self.projects[0].name)
        if state.config is not None:
            self.workspace_path_var.set(state.config.path)
            self.git_remote_var.set(state.config.git_remote or "")
            self.git_branch_var.set(state.config.git_branch or "")
        if state.configured:
            self._set_workspace_status("项目路径已配置并可访问。", SUCCESS)
        elif state.config is not None:
            self._set_workspace_status("已保存的项目路径当前不可访问，请重新选择。", DANGER)
        else:
            self._set_workspace_status("尚未配置项目路径。", MUTED)

    def _detect_model(self) -> None:
        self._load_model_state(
            self.model_service.inspect(self.model_command_var.get().strip() or None)
        )

    def _browse_model(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 Claude Code 可执行文件",
            parent=self.window,
            filetypes=[("Claude Code", "claude.exe"), ("可执行程序", "*.exe")],
        )
        if selected:
            self.model_command_var.set(selected)
            self._detect_model()

    def _save_model(self) -> None:
        try:
            state = self.model_service.save(
                command=self.model_command_var.get(),
                endpoint=self.model_endpoint_var.get(),
                model=self.model_name_var.get(),
                api_key=self.model_key_var.get(),
            )
        except ModelSetupError as exc:
            messagebox.showerror("模型配置未保存", str(exc), parent=self.window)
            return
        except Exception as exc:
            messagebox.showerror("模型配置失败", str(exc), parent=self.window)
            return
        self.model_state = state
        self._load_model_state(state)
        self._set_model_status("模型配置已保存并立即生效。", SUCCESS)
        if self.on_model_saved:
            self.on_model_saved(state)
        if self.required_model_setup:
            self.window.grab_release()
            self.window.destroy()

    def _collect_embedding_config(self) -> EmbeddingConnectionConfig:
        return self.embedding_service.build_config(
            endpoint=self.embedding_endpoint_var.get(),
            model=self.embedding_model_var.get(),
            output_dimension=self.embedding_dimension_var.get(),
        )

    def _test_embedding(self) -> None:
        if self._testing_embedding:
            return
        try:
            config = self._collect_embedding_config()
        except Exception as exc:
            messagebox.showerror("Embedding 配置不完整", str(exc), parent=self.window)
            return
        api_key = self.embedding_key_var.get()
        self._set_embedding_busy(True)
        self._set_embedding_status("正在测试 Voyage Embedding 连接…", ACCENT)

        def work() -> None:
            try:
                self._embedding_test_results.put(
                    ("success", self.embedding_service.test(config, api_key))
                )
            except Exception as exc:
                self._embedding_test_results.put(("error", exc))

        threading.Thread(target=work, daemon=True).start()
        self.window.after(100, self._drain_embedding_test)

    def _drain_embedding_test(self) -> None:
        if not self.window.winfo_exists():
            return
        try:
            kind, payload = self._embedding_test_results.get_nowait()
        except queue.Empty:
            self.window.after(100, self._drain_embedding_test)
            return
        self._set_embedding_busy(False)
        if kind == "success":
            self._set_embedding_status(str(payload), SUCCESS)
        else:
            self._set_embedding_status(f"连接失败 · {payload}", DANGER)

    def _save_embedding(self) -> None:
        if self._testing_embedding:
            return
        try:
            state = self.embedding_service.save(
                self._collect_embedding_config(),
                self.embedding_key_var.get(),
            )
        except EmbeddingSetupError as exc:
            messagebox.showerror("Embedding 配置未保存", str(exc), parent=self.window)
            return
        except Exception as exc:
            messagebox.showerror("Embedding 配置失败", str(exc), parent=self.window)
            return
        self._load_embedding_state(state)
        self._set_embedding_status(
            "Embedding 配置已保存；新任务和新建索引将使用该配置。",
            SUCCESS,
        )
        if self.on_embedding_saved:
            self.on_embedding_saved(state)

    def _collect_database_config(self) -> SQLServerConnectionConfig:
        return build_connection_config(
            server=self.db_server_var.get(),
            port=self.db_port_var.get(),
            database=self.db_name_var.get(),
            driver=self.db_driver_var.get(),
            authentication=self.db_auth_var.get(),
            username=self.db_username_var.get(),
            encrypt=self.db_encrypt_var.get(),
            trust_server_certificate=self.db_trust_var.get(),
        )

    def _test_database(self) -> None:
        if self._testing_database:
            return
        try:
            config = self._collect_database_config()
        except Exception as exc:
            messagebox.showerror("连接配置不完整", str(exc), parent=self.window)
            return
        password = self.db_password_var.get()
        connection_service = self._selected_connection_service()
        self._set_database_busy(True)
        self._set_database_status("正在测试连接…", ACCENT)

        def work() -> None:
            try:
                self._test_results.put(
                    ("success", connection_service.test(config, password))
                )
            except Exception as exc:
                self._test_results.put(("error", exc))

        threading.Thread(target=work, daemon=True).start()
        self.window.after(100, self._drain_database_test)

    def _drain_database_test(self) -> None:
        if not self.window.winfo_exists():
            return
        try:
            kind, payload = self._test_results.get_nowait()
        except queue.Empty:
            self.window.after(100, self._drain_database_test)
            return
        self._set_database_busy(False)
        if kind == "success":
            self._set_database_status(str(payload), SUCCESS)
        else:
            self._set_database_status(f"连接失败 · {payload}", DANGER)

    def _save_database(self) -> None:
        if self._testing_database:
            return
        if self._editing_source_id is not None:
            try:
                config = self._collect_database_config()
                definition = self.datasource_service.load().sources.get(self._editing_source_id)
                if definition is None:
                    definition = DataSourceDefinition(
                        datasource_id=f"ds-{uuid4().hex}", name="新数据源", environment="未指定",
                    )
            except Exception as exc:
                messagebox.showerror("数据源未保存", str(exc), parent=self.window)
                return
            password = self.db_password_var.get()

            def save_source(updated):
                if updated.environment == "未指定":
                    raise ValueError("请明确填写此数据源的实际环境")
                self.datasource_service.save_source(updated, config, password)
                self._editing_source_id = updated.datasource_id
                self.db_password_var.set("")
                self._refresh_sources()
                self._load_database_state(self._selected_connection_service().inspect())
                self._set_database_status("数据源已保存，请在项目维护中绑定后新建任务。", SUCCESS)
                if self.on_datasources_changed is not None:
                    self.on_datasources_changed()

            edit_source(self.window, definition, save_source)
            return
        try:
            state = self.database_service.save(
                self._collect_database_config(),
                self.db_password_var.get(),
            )
        except Exception as exc:
            messagebox.showerror("数据库配置未保存", str(exc), parent=self.window)
            return
        self.db_password_var.set("")
        self._load_database_state(state)
        self._set_database_status("SQL Server 配置已保存，将用于两套流程的新任务。", SUCCESS)
        if self.on_database_saved:
            self.on_database_saved(state)

    def _browse_workspace(self) -> None:
        initial = self.workspace_path_var.get().strip()
        if not initial or not Path(initial).is_dir():
            initial = str(Path.cwd())
        selected = filedialog.askdirectory(
            title="选择项目代码根目录",
            initialdir=initial,
            mustexist=True,
            parent=self.window,
        )
        if selected:
            self.workspace_path_var.set(str(Path(selected).resolve()))

    def _new_workspace_project(self) -> None:
        self._editing_project_id = None
        self.workspace_choice_var.set("")
        for variable in (self.workspace_name_var, self.workspace_path_var,
                         self.git_remote_var, self.git_branch_var):
            variable.set("")
        self._set_workspace_status("保存项目后自动创建开发和异常两份空知识文件。", MUTED)

    def _select_workspace_project(self, _event: tk.Event[tk.Misc]) -> None:
        project = next(p for p in self.projects if p.name == self.workspace_choice_var.get())
        self._load_workspace_state(WorkspaceConfigState(
            config=project, available=Path(project.path).is_dir(),
        ))

    def _save_workspace(self) -> None:
        if not self._confirm_discard_knowledge():
            return
        try:
            project = self.workspace_service.save_project(
                self.workspace_name_var.get(), self.workspace_path_var.get(),
                self.git_remote_var.get().strip(), self.git_branch_var.get().strip(),
                project_id=self._editing_project_id,
            )
            self.projects = self.workspace_service.list_projects()
            self.workspace_combo.configure(values=[p.name for p in self.projects])
        except Exception as exc:
            messagebox.showerror("项目未保存", str(exc), parent=self.window)
            return
        state = WorkspaceConfigState(config=project, available=True)
        self._load_workspace_state(state)
        self._refresh_knowledge(preferred_branch=project.name)
        self._set_workspace_status("项目已保存，入口定位、开发和异常知识文件均已准备。", SUCCESS)
        if self.on_workspace_saved:
            self.on_workspace_saved(state)

    def _knowledge_domain(self) -> KnowledgeDomain:
        return {
            "入口定位": KnowledgeDomain.NAVIGATION,
            "异常处理": KnowledgeDomain.INCIDENT,
            "开发": KnowledgeDomain.DEVELOPMENT,
        }[self.knowledge_domain_var.get()]

    def _refresh_knowledge(
        self,
        *,
        preferred_branch: str = "",
        confirm_discard: bool = False,
    ) -> None:
        if confirm_discard and not self._confirm_discard_knowledge():
            self._restore_loaded_knowledge_selection()
            return
        try:
            self.projects = self.workspace_service.list_projects()
            branches = [p.name for p in self.projects]
            self.knowledge_branch_combo.configure(values=branches)
            selected_branch = preferred_branch or self.knowledge_branch_var.get()
            if selected_branch not in branches:
                selected_branch = branches[0] if branches else ""
            self.knowledge_branch_var.set(selected_branch)
            if selected_branch:
                self._load_knowledge_branch()
            else:
                self._clear_knowledge_editor()
                self.knowledge_path_var.set("")
                self._set_knowledge_status("请先在项目维护中新增项目。", MUTED)
        except Exception as exc:
            self.knowledge_branch_combo.configure(values=[])
            self.knowledge_branch_var.set("")
            self.knowledge_path_var.set("")
            self._clear_knowledge_editor()
            self._set_knowledge_status(f"无法读取 Markdown 配置：{exc}", DANGER)

    def _load_knowledge_branch(self) -> None:
        try:
            domain = self._knowledge_domain()
            branch = self.knowledge_branch_var.get()
            project = next(p for p in self.projects if p.name == branch)
            path, content = self.knowledge_service.load_branch(domain, project.id)
        except Exception as exc:
            self._set_knowledge_status(f"无法读取 Markdown：{exc}", DANGER)
            return
        self.knowledge_editor.delete("1.0", "end")
        self.knowledge_editor.insert("1.0", content)
        self.knowledge_editor.edit_modified(False)
        self.knowledge_path_var.set(self.knowledge_service.relative_path(path))
        self._loaded_knowledge = (domain.value, branch)
        self._set_knowledge_status("已加载该项目的唯一知识文档，可编辑或保存为空。", SUCCESS)

    def _clear_knowledge_editor(self) -> None:
        self.knowledge_editor.delete("1.0", "end")
        self.knowledge_editor.edit_modified(False)
        self._loaded_knowledge = None

    def _save_knowledge(self) -> bool:
        try:
            path = self.knowledge_service.save_branch(
                self._knowledge_domain(),
                next(p.id for p in self.projects if p.name == self.knowledge_branch_var.get()),
                self.knowledge_editor.get("1.0", "end-1c"),
            )
        except WorkspaceKnowledgeError as exc:
            messagebox.showerror("Markdown 未保存", str(exc), parent=self.window)
            return False
        except Exception as exc:
            messagebox.showerror("保存 Markdown 失败", str(exc), parent=self.window)
            return False
        self.knowledge_editor.edit_modified(False)
        self.knowledge_path_var.set(self.knowledge_service.relative_path(path))
        self._loaded_knowledge = (
            self._knowledge_domain().value,
            self.knowledge_branch_var.get(),
        )
        self._set_knowledge_status("知识已保存；下次调用自动加载，RAG 需手动重建。", SUCCESS)
        return True

    def _on_knowledge_domain_changed(self, _event: tk.Event[tk.Misc]) -> None:
        self._refresh_knowledge(confirm_discard=True)

    def _on_knowledge_branch_changed(self, _event: tk.Event[tk.Misc]) -> None:
        self._refresh_knowledge(
            preferred_branch=self.knowledge_branch_var.get(),
            confirm_discard=True,
        )

    def _on_knowledge_modified(self, _event: tk.Event[tk.Misc]) -> None:
        if self.knowledge_editor.edit_modified():
            self._set_knowledge_status("内容已修改，尚未保存。", ACCENT)

    def _confirm_discard_knowledge(self) -> bool:
        if not self.knowledge_editor.edit_modified():
            return True
        return messagebox.askyesno(
            "尚未保存",
            "当前 Markdown 有未保存修改，是否放弃这些修改？",
            parent=self.window,
        )

    def _restore_loaded_knowledge_selection(self) -> None:
        if self._loaded_knowledge is None:
            return
        domain, branch = self._loaded_knowledge
        self.knowledge_domain_var.set(
            {"incident": "异常处理", "navigation": "入口定位", "development": "开发"}[domain]
        )
        self.knowledge_branch_var.set(branch)

    def _set_knowledge_status(self, message: str, color: str) -> None:
        self.knowledge_status_var.set(message)
        self.knowledge_status_label.configure(fg=color)

    def _sync_database_auth(self) -> None:
        sql_auth = self.db_auth_var.get() == SQLServerAuthentication.SQL_PASSWORD.value
        state = "normal" if sql_auth and not self._testing_database else "disabled"
        self.db_username_entry.configure(state=state)
        self.db_password_entry.configure(state=state)

    def _set_database_busy(self, busy: bool) -> None:
        self._testing_database = busy
        self.source_combo.configure(state="disabled" if busy else "readonly")
        state = "disabled" if busy else "normal"
        for control in (
            self.db_server_entry,
            self.db_port_entry,
            self.db_name_entry,
            self.database_test_button,
            self.database_save_button,
        ):
            control.configure(state=state)
        self.db_driver_combo.configure(
            state="disabled" if busy or not self.database_drivers else "readonly"
        )
        for control in [*self.db_auth_buttons, *self.db_option_buttons]:
            control.configure(state=state)
        self._sync_database_auth()

    def _set_embedding_busy(self, busy: bool) -> None:
        self._testing_embedding = busy
        state = "disabled" if busy else "normal"
        for control in (
            self.embedding_endpoint_entry,
            self.embedding_model_entry,
            self.embedding_key_entry,
            self.embedding_test_button,
            self.embedding_save_button,
        ):
            control.configure(state=state)
        self.embedding_dimension_combo.configure(state="disabled" if busy else "normal")

    def _set_model_status(self, message: str, color: str) -> None:
        self.model_status_var.set(message)
        self.model_status_label.configure(fg=color)

    def _set_embedding_status(self, message: str, color: str) -> None:
        self.embedding_status_var.set(message)
        self.embedding_status_label.configure(fg=color)

    def _set_database_status(self, message: str, color: str) -> None:
        self.database_status_var.set(message)
        self.database_status_label.configure(fg=color)

    def _set_workspace_status(self, message: str, color: str) -> None:
        self.workspace_status_var.set(message)
        self.workspace_status_label.configure(fg=color)

    @staticmethod
    def _status(parent: tk.Misc, variable: tk.StringVar, row: int) -> tk.Label:
        label = tk.Label(
            parent,
            textvariable=variable,
            font=("Microsoft YaHei UI", 8),
            fg=MUTED,
            bg=PANEL,
            anchor="w",
            padx=10,
            pady=6,
        )
        label.grid(row=row, column=0, sticky="ew", padx=26, pady=(9, 5))
        return label

    @staticmethod
    def _field_label(parent: tk.Misc, text: str, row: int) -> None:
        tk.Label(
            parent,
            text=text,
            font=("Microsoft YaHei UI", 8, "bold"),
            fg=TEXT,
            bg=CARD,
            anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=26, pady=(5, 3))

    @staticmethod
    def _inline_label(parent: tk.Misc, text: str, column: int) -> None:
        tk.Label(
            parent,
            text=text,
            font=("Microsoft YaHei UI", 8, "bold"),
            fg=TEXT,
            bg=CARD,
            anchor="w",
        ).grid(row=0, column=column, sticky="w", pady=(0, 4))

    @staticmethod
    def _inline_row_label(parent: tk.Misc, text: str, column: int) -> None:
        tk.Label(
            parent,
            text=text,
            font=("Microsoft YaHei UI", 8, "bold"),
            fg=TEXT,
            bg=CARD,
            anchor="w",
        ).grid(row=0, column=column, sticky="w")

    @staticmethod
    def _entry(
        parent: tk.Misc,
        variable: tk.StringVar,
        *,
        show: str = "",
        width: int = 0,
    ) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            show=show,
            width=width,
            font=("Microsoft YaHei UI", 9),
            fg=TEXT,
            bg="#FFFFFF",
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )

    @staticmethod
    def _button(
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        background: str,
        foreground: str,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=("Microsoft YaHei UI", 9, "bold"),
            fg=foreground,
            bg=background,
            activeforeground=foreground,
            activebackground=background,
            relief="flat",
            borderwidth=0,
            padx=18,
            pady=10,
            cursor="hand2",
        )

    def _close(self) -> None:
        if self._testing_database or self._testing_embedding:
            return
        if not self._confirm_discard_knowledge():
            return
        if self.required_model_setup and not self.model_service.inspect().ready:
            self.window.grab_release()
            self.window.destroy()
            self.parent.destroy()
            return
        self.window.grab_release()
        self.window.destroy()
