"""数据源元信息与项目授权编辑；连接表单复用原系统配置页面。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from autocoding_agent.datasource_models import DataSourceDefinition, ProjectDataSources


def edit_source(parent, definition: DataSourceDefinition, save) -> None:
    window = tk.Toplevel(parent)
    window.title("数据源信息 · SQL Server")
    window.transient(parent)
    window.grab_set()
    fields = {}
    for row, (key, label) in enumerate([
        ("name", "名称"), ("environment", "环境（开发／测试／生产等）"),
        ("schema_name", "Schema（例如 dbo）"), ("subproject", "所属子项目"),
        ("purpose", "用途（菜单／业务／日志等）"),
    ]):
        ttk.Label(window, text=label).grid(row=row, column=0, padx=12, pady=8, sticky="w")
        variable = tk.StringVar(value=getattr(definition, key))
        ttk.Entry(window, textvariable=variable, width=45).grid(
            row=row, column=1, padx=12, pady=8,
        )
        fields[key] = variable
    ttk.Label(
        window, text="保存后需在项目维护中绑定；启用目录后，新任务不再使用旧全局连接。",
    ).grid(row=5, columnspan=2, padx=12, pady=8)

    def commit():
        try:
            updated = DataSourceDefinition.model_validate({
                **definition.model_dump(),
                **{key: value.get().strip() for key, value in fields.items()},
            })
            save(updated)
        except Exception as exc:
            messagebox.showerror("数据源未保存", str(exc), parent=window)
            return
        window.destroy()
        parent.grab_set()

    ttk.Button(window, text="保存数据源", command=commit).grid(
        row=6, columnspan=2, padx=12, pady=12,
    )


def edit_project_sources(parent, service, project: str, name: str, on_saved=None) -> None:
    catalog = service.load()
    binding = catalog.projects.get(project, ProjectDataSources())
    sources = list(catalog.sources.values())
    window = tk.Toplevel(parent)
    window.title(f"项目数据源 · {name}")
    window.transient(parent)
    window.grab_set()
    ttk.Label(window, text="选择允许查询的数据源；未绑定的库不会提供给模型。").pack(
        padx=16, pady=12, anchor="w",
    )
    choices = tk.Listbox(window, selectmode="multiple", exportselection=False, width=85, height=10)
    choices.pack(padx=16, fill="both", expand=True)
    for index, source in enumerate(sources):
        choices.insert("end", (
            f"{source.name} | {source.environment} | {source.subproject} | {source.purpose}"
        ))
        if source.datasource_id in binding.source_ids:
            choices.selection_set(index)
    labels = ["不指定（存在歧义时询问）"] + [
        f"{item.name} / {item.environment} [{item.datasource_id}]" for item in sources
    ]
    defaults = []
    for text, selected in [
        ("默认菜单库", binding.menu_default), ("默认业务库", binding.business_default),
    ]:
        ttk.Label(window, text=text).pack(padx=16, pady=(10, 2), anchor="w")
        combo = ttk.Combobox(window, values=labels, state="readonly", width=80)
        ids = [None] + [item.datasource_id for item in sources]
        combo.current(ids.index(selected) if selected in ids else 0)
        combo.pack(padx=16, fill="x")
        defaults.append(combo)
    ttk.Label(window, text=(
        "保存供新任务使用；已有任务的绑定变化会阻止继续查询，需核对后新建任务。"
    )).pack(
        padx=16, pady=12,
    )

    def commit():
        try:
            updated = ProjectDataSources(
                source_ids=[sources[index].datasource_id for index in choices.curselection()],
                menu_default=ids[defaults[0].current()],
                business_default=ids[defaults[1].current()],
            )
            service.bind_project(project, updated)
            if on_saved is not None:
                on_saved()
        except Exception as exc:
            messagebox.showerror("绑定未保存", str(exc), parent=window)
            return
        window.destroy()
        parent.grab_set()

    ttk.Button(window, text="保存绑定", command=commit).pack(pady=(0, 12))
