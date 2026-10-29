"""Oracle 连接编辑；连接测试在后台执行，凭据不回显。"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from uuid import uuid4

from autocoding_agent.datasource_models import DataSourceDefinition
from autocoding_agent.oracle_config import OracleConnectionConfig


class OracleSettingsDialog:
    def __init__(self, parent, service, definition=None, on_saved=None) -> None:
        self.parent = parent
        self.service = service
        self.definition = definition
        self.on_saved = on_saved
        self.source_id = definition.datasource_id if definition else f"ds-{uuid4().hex}"
        self.connection = service.connection(self.source_id, "oracle")
        state = self.connection.inspect()
        self.window = tk.Toplevel(parent)
        self.window.title("Oracle 数据源")
        self.window.transient(parent)
        self.window.grab_set()
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.variables = {}
        self.controls = []
        values = definition.model_dump() if definition else {}
        if state.config is not None:
            values.update(state.config.model_dump())
        values.setdefault("port", 1521)
        values.setdefault("mode", "thin")
        values.setdefault("identifier_type", "service_name")
        values.setdefault("connection_timeout_seconds", 10)
        fields = [
            ("name", "数据源名称"),
            ("environment", "实际环境（开发／测试／生产）"),
            ("schema_name", "Owner / Schema"),
            ("subproject", "所属子项目"),
            ("purpose", "用途"),
            ("server", "监听地址"),
            ("port", "端口"),
            ("identifier_type", "连接标识类型"),
            ("database", "Service Name / SID"),
            ("username", "只读账号"),
            ("password", "密码（已保存时留空保持）"),
            ("mode", "驱动模式"),
            ("client_lib_dir", "Thick 客户端目录（Thin 留空）"),
            ("connection_timeout_seconds", "连接超时（秒）"),
        ]
        for row, (key, label) in enumerate(fields):
            ttk.Label(self.window, text=label).grid(row=row, column=0, padx=12, pady=4, sticky="w")
            variable = tk.StringVar(value=str(values.get(key, "")))
            self.variables[key] = variable
            if key in {"mode", "identifier_type"}:
                options = ["thin", "thick"] if key == "mode" else ["service_name", "sid"]
                control = ttk.Combobox(
                    self.window,
                    textvariable=variable,
                    values=options,
                    state="readonly",
                    width=46,
                )
            else:
                control = ttk.Entry(
                    self.window,
                    textvariable=variable,
                    width=49,
                    show="●" if key == "password" else "",
                )
            control.grid(row=row, column=1, padx=12, pady=4, sticky="ew")
            self.controls.append(control)
        ttk.Label(
            self.window,
            text=(
                "Oracle 19c 可先选 thin；无需 JDBC JAR 或 Instant Client。\n"
                "thick 需 Oracle Client 19+；同一进程模式须一致，切换后重启。"
            ),
        ).grid(row=len(fields), columnspan=2, padx=12, pady=8, sticky="w")
        self.status = tk.StringVar(
            value="密码已保存，留空保持" if state.has_password else "请填写连接信息"
        )
        ttk.Label(self.window, textvariable=self.status, wraplength=620).grid(
            row=len(fields) + 1,
            columnspan=2,
            padx=12,
            pady=8,
            sticky="w",
        )
        buttons = ttk.Frame(self.window)
        buttons.grid(row=len(fields) + 2, columnspan=2, padx=12, pady=12)
        self.test_button = ttk.Button(buttons, text="测试连接", command=self._test)
        self.test_button.pack(side="left", padx=8)
        self.save_button = ttk.Button(buttons, text="保存数据源", command=self._save)
        self.save_button.pack(side="left", padx=8)
        self.results = queue.Queue()
        self.testing = False

    def _config(self):
        names = OracleConnectionConfig.model_fields
        return OracleConnectionConfig.model_validate(
            {key: self.variables[key].get() for key in names}
        )

    def _test(self):
        if self.testing:
            return
        try:
            config = self._config()
        except ValueError:
            self.status.set("请检查地址、端口、连接标识、账号及模式配置")
            return
        password = self.variables["password"].get()
        self.testing = True
        for control in [*self.controls, self.test_button, self.save_button]:
            control.configure(state="disabled")
        self.status.set("正在连接并读取数据库版本…")

        def work():
            try:
                result = self.connection.test(config, password)
            except Exception as exc:
                result = str(exc)
            self.results.put(result)

        threading.Thread(target=work, daemon=True).start()
        self.window.after(100, self._poll)

    def _poll(self):
        if not self.window.winfo_exists():
            return
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            self.window.after(100, self._poll)
            return
        self.testing = False
        self.status.set(result)
        for control in [*self.controls, self.test_button, self.save_button]:
            control.configure(state="readonly" if isinstance(control, ttk.Combobox) else "normal")

    def _save(self):
        if self.testing:
            return
        try:
            config = self._config()
            definition = DataSourceDefinition(
                datasource_id=self.source_id,
                database_type="oracle",
                **{
                    key: self.variables[key].get()
                    for key in ("name", "environment", "schema_name", "subproject", "purpose")
                },
            )
            self.service.save_source(definition, config, self.variables["password"].get())
            self.variables["password"].set("")
            if self.on_saved is not None:
                self.on_saved()
        except Exception as exc:
            messagebox.showerror("Oracle 数据源未保存", str(exc), parent=self.window)
            return
        self._close()

    def _close(self):
        self.window.destroy()
        if self.parent.winfo_exists():
            self.parent.grab_set()
