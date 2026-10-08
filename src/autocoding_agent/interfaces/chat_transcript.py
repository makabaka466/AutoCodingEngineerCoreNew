"""Readable, lossless presentation of chat messages in the existing Tk text widget."""

from __future__ import annotations

import hashlib
import re
import tkinter as tk

_SECTION_TITLES = {
    "异常总结", "解决方案", "依据", "变更文件", "验证", "数据查询", "能力文档",
    "发现", "定位页面", "页面代码", "关联代码", "页面匹配依据", "解决方法",
    "修改内容", "目标效果", "影响与边界", "验证计划", "预览", "修改方案",
}
_INLINE = re.compile(r"(\*\*[^\n]+?\*\*|`[^`\n]+`)")


def insert_markdown(widget: tk.Text, content: str, base: str) -> None:
    """Render a conservative Markdown subset; retain unsupported syntax as text."""
    fenced = False
    for line in content.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            widget.insert("end", line + "\n", (base, "chat_code"))
            continue
        if not line.strip():
            widget.insert("end", "\n", "chat_gap")
            continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+)$", line)
        if heading or line.strip() in _SECTION_TITLES:
            widget.insert("end", (heading.group(1) if heading else line) + "\n",
                          (base, "chat_heading"))
            continue
        if re.fullmatch(r"\s*([-*_])\1{2,}\s*", line):
            widget.insert("end", "────────────────────────\n", "chat_muted")
            continue
        bullet = re.match(r"^(\s*)[-*]\s+", line)
        if bullet:
            line = bullet.group(1) + "  • " + line[bullet.end():]
        for part in _INLINE.split(line):
            style = "chat_bold" if part.startswith("**") else (
                "chat_inline_code" if part.startswith("`") else None
            )
            text = part[2:-2] if style == "chat_bold" else part[1:-1] if style else part
            widget.insert("end", text, (base, style) if style else (base,))
        widget.insert("end", "\n", base)


class ChatTranscript:
    """Fold presentation only; copied text and persisted model responses stay complete."""

    def __init__(self, widget: tk.Text) -> None:
        self.widget = widget
        self.entries: list[tuple[str, str]] = []
        self.expanded: set[str] = set()
        self.links: list[str] = []
        self.details_open = False
        for name, options in {
            "chat_heading": {"font": ("Microsoft YaHei UI", 12, "bold"),
                             "spacing1": 8, "spacing3": 4},
            "chat_bold": {"font": ("Microsoft YaHei UI", 10, "bold")},
            "chat_code": {"font": ("Consolas", 10), "background": "#F2F4F7",
                          "lmargin1": 28, "lmargin2": 28, "spacing1": 1, "spacing3": 1},
            "chat_inline_code": {"font": ("Consolas", 10), "background": "#EEF2F6"},
            "chat_muted": {"foreground": "#667085", "font": ("Microsoft YaHei UI", 9)},
            "chat_gap": {"font": ("Microsoft YaHei UI", 4), "spacing1": 0, "spacing3": 0},
        }.items():
            widget.tag_configure(name, **options)

    @staticmethod
    def key(index: int, role: str, content: str) -> str:
        return hashlib.sha256(f"{index}:{role}:{content.strip()}".encode()).hexdigest()

    def _link(self, label: str, callback) -> None:
        tag = f"chat_link_{len(self.links)}"
        self.links.append(tag)
        self.widget.tag_configure(tag, foreground="#3659C9",
                                  font=("Microsoft YaHei UI", 9), spacing1=6, spacing3=8)
        self.widget.tag_bind(tag, "<Button-1>", lambda _event: callback())
        self.widget.tag_bind(tag, "<Enter>", lambda _event: self.widget.configure(cursor="hand2"))
        self.widget.tag_bind(tag, "<Leave>", lambda _event: self.widget.configure(cursor="xterm"))
        self.widget.insert("end", label, tag)

    def _copy(self, content: str) -> None:
        self.widget.clipboard_clear()
        self.widget.clipboard_append(content)

    def toggle(self, key: str) -> None:
        self.expanded.symmetric_difference_update({key})
        self.render(self.entries, details_open=self.details_open, preserve_scroll=True)

    def expand_all(self) -> None:
        self.expanded = {
            self.key(i, role, content) for i, (role, content) in enumerate(self.entries)
        }
        self.render(self.entries, details_open=self.details_open, preserve_scroll=True)

    def collapse_all(self) -> None:
        self.expanded.clear()
        self.render(self.entries, details_open=False, preserve_scroll=True)

    def render(self, entries: list[tuple[str, str]], *, details_open: bool = False,
               preserve_scroll: bool = False) -> None:
        position = self.widget.yview()[0]
        self.entries = entries
        self.details_open = details_open
        valid = {self.key(i, role, content) for i, (role, content) in enumerate(entries)}
        self.expanded.intersection_update(valid)
        widget = self.widget
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for tag in self.links:
            widget.tag_delete(tag)
        self.links.clear()
        for tag in ("assistant_message", "system_message", "metadata"):
            widget.tag_configure(tag, spacing1=1, spacing3=3)
        names = {"user": "你", "assistant": "Agent", "system": "系统记录",
                 "metadata": "调查与验证详情"}
        latest = max((i for i, item in enumerate(entries) if item[0] == "assistant"), default=-1)
        widget.mark_set("latest_reply", "1.0")
        for index, (role, content) in enumerate(entries):
            content = content.strip()
            key = self.key(index, role, content)
            long = role != "user" and (len(content) > 1200 or len(content.splitlines()) > 24)
            foldable = long or role in {"metadata", "system"}
            opened = key in self.expanded or (details_open and role in {"metadata", "system"})
            if index == latest:
                widget.mark_set("latest_reply", widget.index("end-1c"))
                widget.mark_gravity("latest_reply", "left")
            widget.insert("end", names[role], f"{role}_name" if role != "metadata"
                          else "chat_heading")
            widget.insert("end", "    ")
            if foldable:
                self._link("收起详情" if opened else "展开全文", lambda key=key: self.toggle(key))
                widget.insert("end", "   ·   ", "chat_muted")
            self._link("复制全文", lambda content=content: self._copy(content))
            widget.insert("end", "\n")
            base = "metadata" if role == "metadata" else f"{role}_message"
            shown = content
            if foldable and not opened:
                if role in {"metadata", "system"}:
                    shown = f"{len(content.splitlines())} 行详情，点击展开查看。"
                else:
                    limit = 600 if index == latest else 300
                    boundary = content.rfind("\n\n", 0, limit)
                    excerpt = content[:boundary if boundary > 120 else limit]
                    shown = "\n".join(excerpt.splitlines()[:14]) + "\n…"
                    if shown.count("```") % 2:
                        shown = shown[:shown.rfind("```")] + "\n…"
            if role == "user":
                widget.insert("end", shown + "\n", base)
            else:
                insert_markdown(widget, shown, base)
            widget.insert("end", "\n\n", "chat_gap")
        widget.tag_raise("sel")
        widget.configure(state="disabled")
        if preserve_scroll:
            widget.yview_moveto(position)
        else:
            if latest >= 0:
                widget.yview("latest_reply")
                widget.after_idle(lambda: widget.yview("latest_reply") if widget.winfo_exists()
                                  else None)
            else:
                widget.see("end")
