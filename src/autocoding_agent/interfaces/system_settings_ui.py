"""One desktop settings window shared by every workflow."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import filedialog, messagebox, ttk

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

WINDOW = "#F7F8FA"
CARD = "#FFFFFF"
PANEL = "#F1F3F5"
BORDER = "#D7DCE2"
TEXT = "#1F2937"
MUTED = "#667085"
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
        required_model_setup: bool = False,
        on_model_saved: Callable[[ModelSetupState], None] | None = None,
        on_database_saved: Callable[[SQLServerConfigState], None] | None = None,
    ) -> None:
        self.parent = parent
        self.model_service = model_service
        self.database_service = database_service
        self.on_model_saved = on_model_saved
        self.on_database_saved = on_database_saved
        self.required_model_setup = required_model_setup
        self.model_state = model_service.inspect()
        self.database_state = database_service.inspect()
        self._test_results: queue.Queue[tuple[str, object]] = queue.Queue()
        self._testing_database = False

        self.window = tk.Toplevel(parent)
        self.window.title("系统配置 · AutoCoding Engineer")
        self.window.configure(bg=WINDOW)
        width, height, geometry = self._geometry(760, 720)
        self.window.geometry(geometry)
        self.window.minsize(min(680, width), min(580, height))
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        if parent.winfo_viewable():
            self.window.transient(parent)

        self._build()
        self._load_model_state(self.model_state)
        self._load_database_state(self.database_state)
        self.notebook.select(self.database_tab if initial_section == "database" else self.model_tab)
        self._sync_footer_actions()
        self.window.grab_set()
        self.window.focus_force()

    def _geometry(self, width: int, height: int) -> tuple[int, int, str]:
        screen_width = self.parent.winfo_screenwidth()
        screen_height = self.parent.winfo_screenheight()
        width = min(width, max(680, screen_width - 40))
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
            text="模型与数据库配置由开发、异常维护流程共同使用。",
            font=("Microsoft YaHei UI", 9),
            fg=MUTED,
            bg=WINDOW,
        ).pack(anchor="w", pady=(3, 0))

        style = ttk.Style(self.window)
        style.configure("Settings.TNotebook", background=WINDOW, borderwidth=0)
        style.configure(
            "Settings.TNotebook.Tab",
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(18, 8),
        )
        self.notebook = ttk.Notebook(
            self.window,
            style="Settings.TNotebook",
        )
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=30, pady=(0, 12))
        self.model_tab = tk.Frame(self.notebook, bg=CARD)
        self.database_tab = tk.Frame(self.notebook, bg=CARD)
        self.notebook.add(self.model_tab, text="模型与 Claude Code")
        self.notebook.add(self.database_tab, text="SQL Server")
        self._build_model_tab()
        self._build_database_tab()

        footer = tk.Frame(self.window, bg=WINDOW)
        footer.grid(row=2, column=0, sticky="ew", padx=30, pady=(0, 18))
        self.close_button = self._button(footer, "关闭", self._close, PANEL, TEXT)
        self.model_save_button = self._button(
            footer, "保存模型配置", self._save_model, ACCENT, "#FFFFFF"
        )
        self.database_test_button = self._button(
            footer, "测试连接", self._test_database, PANEL, TEXT
        )
        self.database_save_button = self._button(
            footer, "保存数据库配置", self._save_database, ACCENT, "#FFFFFF"
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

    def _sync_footer_actions(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        for button in (
            self.close_button,
            self.model_save_button,
            self.database_test_button,
            self.database_save_button,
        ):
            button.pack_forget()
        self.close_button.pack(side="right")
        if self.notebook.select() == str(self.database_tab):
            self.database_save_button.pack(side="right", padx=(0, 8))
            self.database_test_button.pack(side="right", padx=(0, 8))
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
        self._set_database_busy(True)
        self._set_database_status("正在测试连接…", ACCENT)

        def work() -> None:
            try:
                self._test_results.put(
                    ("success", self.database_service.test(config, password))
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

    def _sync_database_auth(self) -> None:
        sql_auth = self.db_auth_var.get() == SQLServerAuthentication.SQL_PASSWORD.value
        state = "normal" if sql_auth and not self._testing_database else "disabled"
        self.db_username_entry.configure(state=state)
        self.db_password_entry.configure(state=state)

    def _set_database_busy(self, busy: bool) -> None:
        self._testing_database = busy
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

    def _set_model_status(self, message: str, color: str) -> None:
        self.model_status_var.set(message)
        self.model_status_label.configure(fg=color)

    def _set_database_status(self, message: str, color: str) -> None:
        self.database_status_var.set(message)
        self.database_status_label.configure(fg=color)

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
            relief="solid",
            borderwidth=1,
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
            padx=16,
            pady=8,
            cursor="hand2",
        )

    def _close(self) -> None:
        if self._testing_database:
            return
        if self.required_model_setup and not self.model_service.inspect().ready:
            self.window.grab_release()
            self.window.destroy()
            self.parent.destroy()
            return
        self.window.grab_release()
        self.window.destroy()
