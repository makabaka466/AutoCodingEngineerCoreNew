"""Desktop management page for explicitly indexing Markdown into the RAG store."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, simpledialog, ttk

from autocoding_agent.knowledge_rag.models import (
    KnowledgeDocument,
    KnowledgeDomain,
    KnowledgeIndexStatus,
)
from autocoding_agent.knowledge_rag.service import KnowledgeRAGService

WINDOW = "#EEF3FA"
CARD = "#F9FBFE"
PANEL = "#F1F5F9"
BORDER = "#DCE5F0"
TEXT = "#111827"
MUTED = "#475569"
ACCENT = "#2563EB"
SUCCESS = "#15803D"
DANGER = "#DC2626"

_STATUS_LABELS = {
    KnowledgeIndexStatus.PENDING: "待加入",
    KnowledgeIndexStatus.INDEXING: "正在索引",
    KnowledgeIndexStatus.INDEXED: "已索引",
    KnowledgeIndexStatus.OUTDATED: "内容已更新",
    KnowledgeIndexStatus.FAILED: "索引失败",
    KnowledgeIndexStatus.REMOVED: "已移除",
}


class KnowledgeManagementDialog:
    def __init__(
        self,
        parent: tk.Misc,
        service: KnowledgeRAGService,
        *,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        self.parent = parent
        self.service = service
        self.on_changed = on_changed
        self.documents: dict[str, KnowledgeDocument] = {}
        self._results: queue.Queue[tuple[str, object]] = queue.Queue()
        self._busy = False

        self.window = tk.Toplevel(parent)
        self.window.title("知识库管理 · AutoCoding Engineer")
        self.window.configure(bg=WINDOW)
        width = min(1180, max(880, parent.winfo_screenwidth() - 100))
        height = min(780, max(620, parent.winfo_screenheight() - 130))
        left = max(0, (parent.winfo_screenwidth() - width) // 2)
        top = max(0, (parent.winfo_screenheight() - height) // 2)
        self.window.geometry(f"{width}x{height}+{left}+{top}")
        self.window.minsize(860, 600)
        if parent.winfo_viewable():
            self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self.status_var = tk.StringVar()
        self.domain_var = tk.StringVar(value="开发")
        self.project_var = tk.StringVar()
        self._build()
        self.refresh()
        self.window.after(100, self._drain_results)
        self.window.grab_set()
        self.window.focus_force()

    def _build(self) -> None:
        self.window.grid_columnconfigure(0, weight=1)
        self.window.grid_rowconfigure(2, weight=1)

        header = tk.Frame(self.window, bg=WINDOW)
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(20, 10))
        header.grid_columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="工程知识库",
            font=("Microsoft YaHei UI", 18, "bold"),
            fg=TEXT,
            bg=WINDOW,
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            header,
            text=(
                "手动选择 Markdown 加入索引；源文件不会被移动或删除。"
                + (
                    ""
                    if self.service.simulated
                    else " 正式模式会把所选 Chunk 和检索文本发送到配置的 Embedding API。"
                )
            ),
            font=("Microsoft YaHei UI", 9),
            fg=MUTED,
            bg=WINDOW,
            justify="left",
            wraplength=620,
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        mode_text = (
            f"模拟模式 · {self.service.model_id} · 后续需用 Qwen3 全量重建"
            if self.service.simulated
            else f"正式模式 · {self.service.model_id}"
        )
        tk.Label(
            header,
            text=mode_text,
            font=("Microsoft YaHei UI", 9, "bold"),
            fg="#B45309" if self.service.simulated else SUCCESS,
            bg="#FFF7ED" if self.service.simulated else "#ECFDF5",
            padx=12,
            pady=7,
        ).grid(row=0, column=1, rowspan=2, sticky="e")

        toolbar = tk.Frame(self.window, bg=WINDOW)
        toolbar.grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 10))
        self.refresh_button = self._button(toolbar, "刷新文档", self.refresh)
        self.preview_button = self._button(toolbar, "预览分块", self.preview_selected)
        self.index_button = self._button(
            toolbar, "加入 / 重建索引", self.index_selected, primary=True
        )
        self.remove_button = self._button(toolbar, "移除索引", self.remove_selected)
        self.test_button = self._button(toolbar, "测试检索", self.test_retrieval)
        for button in (
            self.refresh_button,
            self.preview_button,
            self.index_button,
            self.remove_button,
            self.test_button,
        ):
            button.pack(side="left", padx=(0, 8))

        body = tk.PanedWindow(
            self.window,
            orient="vertical",
            bg=WINDOW,
            sashwidth=6,
            sashrelief="flat",
            borderwidth=0,
        )
        body.grid(row=2, column=0, sticky="nsew", padx=28, pady=(0, 10))
        list_card = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        preview_card = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        body.add(list_card, minsize=260, stretch="always")
        body.add(preview_card, minsize=180, stretch="always")

        list_card.grid_columnconfigure(0, weight=1)
        list_card.grid_rowconfigure(0, weight=1)
        columns = ("status", "source", "domain", "project", "chunks", "path")
        self.tree = ttk.Treeview(
            list_card,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        headings = {
            "status": "状态",
            "source": "来源",
            "domain": "领域",
            "project": "项目",
            "chunks": "Chunk",
            "path": "文档路径",
        }
        widths = {
            "status": 90,
            "source": 150,
            "domain": 80,
            "project": 100,
            "chunks": 60,
            "path": 470,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=50,
                anchor="w" if column != "chunks" else "center",
            )
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=10)
        tree_scroll = ttk.Scrollbar(list_card, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=10)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._show_document_summary)

        preview_card.grid_columnconfigure(0, weight=1)
        preview_card.grid_rowconfigure(1, weight=1)
        filter_row = tk.Frame(preview_card, bg=CARD)
        filter_row.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        tk.Label(filter_row, text="测试领域", fg=MUTED, bg=CARD).pack(side="left")
        ttk.Combobox(
            filter_row,
            textvariable=self.domain_var,
            values=["开发", "异常处理", "通用"],
            state="readonly",
            width=10,
        ).pack(side="left", padx=(8, 16))
        tk.Label(filter_row, text="项目过滤", fg=MUTED, bg=CARD).pack(side="left")
        tk.Entry(
            filter_row,
            textvariable=self.project_var,
            bg=PANEL,
            fg=TEXT,
            relief="flat",
            width=18,
        ).pack(side="left", padx=(8, 0), ipady=4)

        self.preview = tk.Text(
            preview_card,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            fg=TEXT,
            bg="#FFFFFF",
            relief="flat",
            padx=12,
            pady=10,
        )
        self.preview.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        footer = tk.Frame(self.window, bg=WINDOW)
        footer.grid(row=3, column=0, sticky="ew", padx=28, pady=(0, 16))
        footer.grid_columnconfigure(0, weight=1)
        self.status_label = tk.Label(
            footer,
            textvariable=self.status_var,
            font=("Microsoft YaHei UI", 8),
            fg=MUTED,
            bg=WINDOW,
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self._button(footer, "关闭", self.close).grid(row=0, column=1)

    def refresh(self) -> None:
        if self._busy:
            return
        try:
            documents = self.service.refresh_documents()
        except Exception as exc:
            messagebox.showerror("无法读取知识文档", str(exc), parent=self.window)
            return
        selected = set(self.tree.selection())
        self.documents = {item.id: item for item in documents}
        self.tree.delete(*self.tree.get_children())
        for document in documents:
            self.tree.insert(
                "",
                "end",
                iid=document.id,
                values=(
                    _STATUS_LABELS[document.status],
                    document.source_type.value,
                    document.domain.value,
                    document.project or "通用",
                    document.chunk_count or "—",
                    document.display_path,
                ),
            )
        for item in selected & self.documents.keys():
            self.tree.selection_add(item)
        pending = sum(
            item.status
            in {
                KnowledgeIndexStatus.PENDING,
                KnowledgeIndexStatus.OUTDATED,
                KnowledgeIndexStatus.FAILED,
            }
            for item in documents
        )
        self._set_status(f"共发现 {len(documents)} 份文档，{pending} 份待加入或重建。")

    def preview_selected(self) -> None:
        selected = self._selected_ids(require_one=True)
        if not selected:
            return
        try:
            chunks = self.service.preview_chunks(selected[0])
        except Exception as exc:
            messagebox.showerror("无法预览分块", str(exc), parent=self.window)
            return
        text = "\n\n".join(
            (
                f"--- Chunk {item.ordinal + 1} · ~{item.approximate_tokens} tokens ---\n"
                f"{item.heading_path or item.title}\n\n{item.content}"
            )
            for item in chunks
        )
        self._show_preview(text or "该文档没有可索引内容。")
        self._set_status(f"分块预览：{len(chunks)} 个 Chunk；尚未写入索引。")

    def index_selected(self) -> None:
        selected = self._selected_ids()
        if not selected:
            return

        def operation() -> list[object]:
            return [self.service.index_document(document_id) for document_id in selected]

        self._run(operation, "正在生成模拟向量并建立 FTS5/向量索引…")

    def remove_selected(self) -> None:
        selected = self._selected_ids()
        if not selected:
            return
        if not messagebox.askyesno(
            "移除知识索引",
            f"从知识库移除所选 {len(selected)} 份文档？源 Markdown 不会被删除。",
            parent=self.window,
        ):
            return

        def operation() -> int:
            for document_id in selected:
                self.service.remove_document(document_id)
            return len(selected)

        self._run(operation, "正在移除索引…")

    def test_retrieval(self) -> None:
        if self._busy:
            return
        query = simpledialog.askstring(
            "测试混合检索",
            "输入工程问题、错误码、类名或方法名：",
            parent=self.window,
        )
        if not query or not query.strip():
            return
        domain = {
            "异常处理": KnowledgeDomain.INCIDENT,
            "通用": KnowledgeDomain.GENERAL,
        }.get(self.domain_var.get(), KnowledgeDomain.DEVELOPMENT)
        try:
            result = self.service.retrieve(
                query,
                domain=domain,
                project=self.project_var.get().strip() or None,
            )
        except Exception as exc:
            messagebox.showerror("检索失败", str(exc), parent=self.window)
            return
        lines = [f"查询：{query}", f"命中：{len(result.hits)}", ""]
        for index, hit in enumerate(result.hits, start=1):
            lines.extend(
                [
                    f"[{index}] score={hit.score:.6f} dense={hit.dense_rank} "
                    f"bm25={hit.lexical_rank}",
                    f"{hit.source_path} > {hit.heading_path or hit.title}",
                    hit.content,
                    "",
                ]
            )
        self._show_preview("\n".join(lines) if result.hits else "没有命中已索引知识。")
        self._set_status("检索完成；当前向量排名为模拟结果，只验证工作流和引用边界。")

    def _show_document_summary(self, _event: tk.Event[tk.Misc]) -> None:
        selected = self.tree.selection()
        if len(selected) != 1:
            return
        document = self.documents.get(selected[0])
        if document is None:
            return
        summary = [
            f"标题：{document.title}",
            f"状态：{_STATUS_LABELS[document.status]}",
            f"来源：{document.source_type.value}",
            f"领域 / 项目：{document.domain.value} / {document.project or '通用'}",
            f"路径：{document.display_path}",
            f"Chunk：{document.chunk_count}",
            f"Embedding：{document.embedding_model or '尚未生成'}",
        ]
        if document.last_error:
            summary.extend(["", f"错误：{document.last_error}"])
        self._show_preview("\n".join(summary))

    def _selected_ids(self, require_one: bool = False) -> list[str]:
        selected = list(self.tree.selection())
        if not selected:
            messagebox.showinfo("选择文档", "请先选择至少一份 Markdown。", parent=self.window)
            return []
        if require_one and len(selected) != 1:
            messagebox.showinfo("选择文档", "分块预览一次只能选择一份文档。", parent=self.window)
            return []
        return selected

    def _run(self, operation: Callable[[], object], label: str) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._set_status(label)

        def worker() -> None:
            try:
                self._results.put(("success", operation()))
            except Exception as exc:
                self._results.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_results(self) -> None:
        if not self.window.winfo_exists():
            return
        try:
            kind, payload = self._results.get_nowait()
        except queue.Empty:
            self.window.after(100, self._drain_results)
            return
        self._set_busy(False)
        if kind == "error":
            messagebox.showerror("知识索引操作失败", str(payload), parent=self.window)
        else:
            self._set_status("知识索引操作已完成。")
            if self.on_changed:
                self.on_changed()
        self.refresh()
        self.window.after(100, self._drain_results)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.refresh_button,
            self.preview_button,
            self.index_button,
            self.remove_button,
            self.test_button,
        ):
            button.configure(state=state)

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _show_preview(self, text: str) -> None:
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    @staticmethod
    def _button(
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        primary: bool = False,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=("Microsoft YaHei UI", 9, "bold" if primary else "normal"),
            fg="#FFFFFF" if primary else TEXT,
            bg=ACCENT if primary else PANEL,
            activebackground="#1D4ED8" if primary else "#E5ECF5",
            activeforeground="#FFFFFF" if primary else TEXT,
            relief="flat",
            borderwidth=0,
            padx=14,
            pady=7,
            cursor="hand2",
        )

    def close(self) -> None:
        if self._busy:
            messagebox.showinfo("知识库管理", "请等待当前索引操作完成。", parent=self.window)
            return
        self.window.grab_release()
        self.window.destroy()
