"""Readable, lossless presentation of chat messages in the existing Tk text widget."""

from __future__ import annotations

import hashlib
import re
import tkinter as tk
import unicodedata

from autocoding_agent.interfaces.desktop_theme import COLORS, RoundedTextBlock

_SECTION_TITLES = {
    "异常总结", "解决方案", "依据", "变更文件", "验证", "数据查询", "能力文档",
    "发现", "定位页面", "页面代码", "关联代码", "页面匹配依据", "解决方法",
    "方案概述", "修改内容", "目标效果", "影响与边界", "验证计划", "预览", "修改方案",
}
_INLINE = re.compile(r"(\*\*[^\n]+?\*\*|`[^`\n]+`)")


def insert_markdown(widget: tk.Text, content: str, base: str, render_code=None) -> None:
    """Render a conservative Markdown subset; retain unsupported syntax as text."""
    fenced = False
    lines = content.splitlines()
    table_widths = []
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            fenced = not fenced
            if fenced and render_code:
                end = next((i for i in range(index+1, len(lines))
                            if lines[i].strip().startswith("```")), len(lines))
                code = "\n".join(lines[index+1:end])
                widget.insert("end", line.strip()[3:] or "代码", "chat_muted")
                widget.insert("end", "\n")
                render_code(code)
                widget.insert("end", "\n")
            continue
        if fenced:
            widget.insert("end", line + "\n", (base, "chat_code", "chat_source")
                          if render_code else (base, "chat_code"))
            continue
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            if not table_widths:
                rows = []
                for row in lines[index:]:
                    if not row.strip().startswith("|"):
                        break
                    rows.append([cell.strip() for cell in row.strip().strip("|").split("|")])
                table_widths = [min(30, max(sum(2 if unicodedata.east_asian_width(c) in "WF"
                                              else 1 for c in row[col])
                                          for row in rows if col < len(row)))
                                for col in range(len(cells))]
            padded = []
            for col, cell in enumerate(cells):
                width = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in cell)
                target = table_widths[col] if col < len(table_widths) else width
                padded.append(cell + " " * max(2, target - width + 2))
            widget.insert("end", "".join(padded).rstrip() + "\n", (base, "chat_table"))
            continue
        table_widths = []
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
        self.timestamps: list[str] = []
        self.expanded: set[str] = set()
        self.links: list[str] = []
        self.details_open = False
        self.blocks: list[RoundedTextBlock] = []
        for name, options in {
            "chat_heading": {"font": ("Microsoft YaHei UI", 12, "bold"),
                             "spacing1": 8, "spacing3": 4},
            "chat_bold": {"font": ("Microsoft YaHei UI", 10, "bold")},
            "chat_code": {"font": ("Consolas", 10), "background": "#F2F4F7",
                          "lmargin1": 28, "lmargin2": 28, "spacing1": 1, "spacing3": 1},
            "chat_inline_code": {"font": ("Consolas", 10), "background": "#EEF2F6"},
            "chat_muted": {"foreground": "#667085", "font": ("Microsoft YaHei UI", 9)},
            "chat_gap": {"font": ("Microsoft YaHei UI", 4), "spacing1": 0, "spacing3": 0},
            "chat_table": {"font": ("Consolas", 10), "background": "#F7F8FA",
                           "spacing1": 5, "spacing3": 5},
        }.items():
            widget.tag_configure(name, **options)
        widget.bind("<Configure>", lambda _: self.resize(), add="+")
        widget.tag_configure("chat_source", elide=True)
        self.resize()

    def resize(self) -> None:
        """正文随可用区域展开，用户气泡保持右对齐；不更改原文或选择复制。"""
        width = max(300, self.widget.winfo_width())
        margin = 12
        for tag in ("assistant_message", "assistant_name", "system_message", "system_name",
                    "metadata", "chat_heading"):
            self.widget.tag_configure(tag, lmargin1=margin, lmargin2=margin, rmargin=margin,
                                      background=COLORS["surface"], foreground=COLORS["text"])
        for tag in ("user_message", "user_name"):
            self.widget.tag_configure(tag, lmargin1=margin + max(60, int(width * .20)),
                                      lmargin2=margin + max(60, int(width * .20)),
                                      rmargin=margin, justify="right")
        self.widget.tag_configure("user_message", background=COLORS["user"],
                                  spacing1=12, spacing3=12)
        self.widget.tag_configure("user_block", justify="right", rmargin=margin)
        for block in self.blocks:
            block_width = width - margin * 2 - 40
            block.configure(width=max(160, block_width if block.code else min(650, block_width)))

    def _insert_block(self, content: str, *, code: bool = False) -> None:
        block = RoundedTextBlock(self.widget, content, code=code)
        self.blocks.append(block)
        start = self.widget.index("end-1c")
        self.widget.window_create("end", window=block)
        self.widget.insert("end", "\n", "assistant_message" if code else "user_block")
        if not code:
            self.widget.tag_add("user_block", start, "end-1c")
            self.widget.tag_raise("user_block")

    @staticmethod
    def key(index: int, role: str, content: str) -> str:
        return hashlib.sha256(f"{index}:{role}:{content.strip()}".encode()).hexdigest()

    def _link(self, label: str, callback) -> None:
        tag = f"chat_link_{len(self.links)}"
        self.links.append(tag)
        self.widget.tag_configure(tag, foreground=COLORS["subtle"],
                                  font=("Microsoft YaHei UI", 9), spacing1=6, spacing3=8)
        self.widget.tag_bind(tag, "<Button-1>", lambda _event: callback())
        self.widget.tag_bind(tag, "<Enter>", lambda _event: (
            self.widget.configure(cursor="hand2"),
            self.widget.tag_configure(tag, foreground=COLORS["text"])))
        self.widget.tag_bind(tag, "<Leave>", lambda _event: (
            self.widget.configure(cursor="xterm"),
            self.widget.tag_configure(tag, foreground=COLORS["subtle"])))
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
        follow_bottom = self.widget.yview()[1] >= .98
        self.entries = entries
        self.details_open = details_open
        valid = {self.key(i, role, content) for i, (role, content) in enumerate(entries)}
        self.expanded.intersection_update(valid)
        widget = self.widget
        widget.configure(state="normal")
        for block in self.blocks:
            block.destroy()
        self.blocks.clear()
        widget.delete("1.0", "end")
        for tag in self.links:
            widget.tag_delete(tag)
        self.links.clear()
        for tag in ("assistant_message", "system_message", "metadata"):
            widget.tag_configure(tag, spacing1=1, spacing3=3)
        names = {"user": "你", "assistant": "ACE", "system": "系统记录",
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
            if index < len(self.timestamps):
                widget.insert("end", self.timestamps[index] + "    ", "chat_muted")
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
                widget.insert("end", shown + "\n", (base, "chat_source"))
                self._insert_block(shown)
            else:
                insert_markdown(widget, shown, base,
                                lambda code: self._insert_block(code, code=True))
            widget.insert("end", "\n\n", "chat_gap")
        widget.tag_raise("sel")
        self.resize()
        widget.configure(state="disabled")
        if preserve_scroll:
            if follow_bottom:
                widget.see("end")
            else:
                widget.yview_moveto(position)
        else:
            if latest >= 0:
                widget.yview("latest_reply")
                widget.after_idle(lambda: widget.yview("latest_reply") if widget.winfo_exists()
                                  else None)
            else:
                widget.see("end")
