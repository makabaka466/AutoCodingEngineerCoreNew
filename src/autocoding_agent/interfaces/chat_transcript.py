"""Readable, lossless presentation of chat messages in the existing Tk text widget."""

from __future__ import annotations

import hashlib
import re
import tkinter as tk
import unicodedata

from autocoding_agent.core.models import MessageAttachment
from autocoding_agent.incident_attachments import numbered_attachments
from autocoding_agent.interfaces.analysis_trace import AnalysisSection
from autocoding_agent.interfaces.attachment_preview import AttachmentTile
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
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+)$", line)
        if heading or line.strip() in _SECTION_TITLES:
            style = "chat_subheading" if heading and len(heading.group(1)) >= 3 else "chat_heading"
            widget.insert("end", (heading.group(2) if heading else line) + "\n", (base, style))
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

    def __init__(self, widget: tk.Text, *, open_analysis=None, detail_view=False) -> None:
        self.detail_view = detail_view
        self.open_analysis = open_analysis
        self.running_section: str | None = None
        self.analysis_bubbles: dict[str, RoundedTextBlock] = {}
        self.widget = widget
        self.entries: list[tuple[str, str]] = []
        self.timestamps: list[str] = []
        self.attachments: dict[int, list[MessageAttachment]] = {}
        self.image_tiles: list[AttachmentTile] = []
        self.expanded: set[str] = set()
        self.links: list[str] = []
        self._link_commands: list[str] = []
        self.details_open = False
        self.blocks: list[RoundedTextBlock] = []
        self.analysis_sections: dict[str, AnalysisSection] = {}
        self.analysis_limits: dict[str, int] = {}
        self.markdown_styles = {
            "chat_heading": {"font": ("Microsoft YaHei UI", 12, "bold"),
                             "spacing1": 8, "spacing3": 4},
            "chat_subheading": {"font": ("Microsoft YaHei UI", 11, "bold"),
                                "spacing1": 10, "spacing3": 4},
            "chat_bold": {"font": ("Microsoft YaHei UI", 10, "bold")},
            "chat_code": {"font": ("Consolas", 10), "background": "#F2F4F7",
                          "lmargin1": 28, "lmargin2": 28, "spacing1": 1, "spacing3": 1},
            "chat_inline_code": {"font": ("Consolas", 10), "background": "#EEF2F6"},
            "chat_muted": {"foreground": "#667085", "font": ("Microsoft YaHei UI", 9)},
            "chat_gap": {"font": ("Microsoft YaHei UI", 4), "spacing1": 0, "spacing3": 0},
            "chat_warning": {"foreground": COLORS["warning"]},
            "chat_table": {"font": ("Consolas", 10), "background": "#F7F8FA",
                           "spacing1": 5, "spacing3": 5},
        }
        for name, options in self.markdown_styles.items():
            widget.tag_configure(name, **options)
        self._width = 0
        self._resize_job = None
        self._latest_job = None
        self._initial_scroll = None
        self._anchors: dict[str, str] = {}
        self._collapsed_details: set[str] = set()
        self._default_details: set[str] = set()
        widget.bind("<Configure>", self._schedule_resize, add="+")
        widget.bind("<Map>", self._on_map, add="+")
        widget.tag_configure("chat_source", elide=True)
        self.resize()

    def _on_map(self, _event) -> None:
        if self._initial_scroll is not None:
            self._latest_job = self.widget.after_idle(self._scroll_after_layout)

    def _scroll_after_layout(self) -> None:
        self._latest_job = None
        if self._initial_scroll is not None and self.widget.winfo_viewable():
            target, self._initial_scroll = self._initial_scroll, None
            self.widget.yview(target)

    def _schedule_resize(self, event) -> None:
        if max(300, event.width) != self._width and self._resize_job is None:
            self._resize_job = self.widget.after_idle(self.resize)

    def resize(self) -> None:
        """正文随可用区域展开，用户气泡保持右对齐；不更改原文或选择复制。"""
        width = max(300, self.widget.winfo_width())
        self._resize_job = None
        if width == self._width:
            return
        self._width = width
        margin = 12
        for tag in ("assistant_message", "assistant_name", "system_message", "system_name",
                    "metadata", "chat_heading", "chat_subheading"):
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
            if not (block.reply or block.code):
                block_width = min(650, block_width)
            block.configure(width=max(160, block_width))

    def _insert_block(self, content: str, *, code: bool = False) -> None:
        block = RoundedTextBlock(self.widget, content, code=code)
        available = max(160, self._width - 64)
        block.configure(width=available if code else min(650, available))
        self.blocks.append(block)
        start = self.widget.index("end-1c")
        self.widget.window_create("end", window=block)
        self.widget.insert("end", "\n", "assistant_message" if code else "user_block")
        if not code:
            self.widget.tag_add("user_block", start, "end-1c")
            self.widget.tag_raise("user_block")

    def _reply_bubble(self, content: str, *, section: str | None = None) -> RoundedTextBlock:
        """正文保留可选择的 Markdown；折叠的小结单独绑定所在轮次。"""
        details = (lambda: self.open_analysis(section)) if section is not None else None
        block = RoundedTextBlock(self.widget, "", reply=True, details=details)
        block.configure(width=max(160, self._width - 64))
        text = block.text
        text.configure(state="normal")
        if section is None:
            for tag, options in self.markdown_styles.items():
                text.tag_configure(tag, **options)
            text.tag_configure("chat_source", elide=True)

            def code_block(code):
                code_box = RoundedTextBlock(text, code, code=True)
                code_box.configure(width=max(100, self._width - 108))
                # 宽度变化或代码首次显示时，外层气泡也要重新计算总高。
                code_box.bind("<Configure>", lambda _: block.after_idle(block._fit_height), add="+")
                text.window_create("end", window=code_box)

            insert_markdown(text, content, "reply", code_block)
            text.delete("end-2c", "end-1c")
        else:
            text.insert("end", content)
            text.configure(fg=COLORS["muted"])
            self.analysis_bubbles[section] = block
        text.configure(state="disabled")
        # 父级 Text 的全选/历史复制需要保留嵌入窗口的可读文字。
        self.widget.insert("end", text.get("1.0", "end-1c") + "\n", "chat_source")
        self.widget.window_create("end", window=block, padx=6)
        self.widget.insert("end", "\n", "chat_gap")
        self.blocks.append(block)
        return block

    def summary(self, key: str) -> str:
        section = self.analysis_sections[key]
        if not section.steps:
            if key == self.running_section:
                return "正在调查，等待首条调查记录…"
            return "本轮未记录调查步骤。"
        # 完成时优先展示本轮最近一次公开的调查结论；运行中展示最新活动。
        last = section.steps[-1]
        if key != self.running_section and not last.failed:
            last = next((step for step in reversed(section.steps)
                         if step.source in {"模型公开说明", "模型判断"}), last)
        summary = " ".join(last.summary.split())
        return summary if len(summary) <= 100 else summary[:99] + "…"

    def update_analysis_summaries(self) -> None:
        """仅修改发生变化的调查摘要，长回复与用户当前的文本选择保留在原控件中。"""
        widget = self.widget
        try:
            for key, block in self.analysis_bubbles.items():
                summary = self.summary(key)
                title = "调查中" if key == self.running_section else "调查过程"
                pos = self._anchors["analysis:" + key]
                if widget.get(pos, pos + " lineend") != title:
                    widget.configure(state="normal")
                    widget.replace(pos, pos + " lineend", title, "chat_muted")
                if block.text.get("1.0", "end-1c") == summary:
                    continue
                block.text.configure(state="normal")
                block.text.replace("1.0", "end-1c", summary)
                block.text.configure(state="disabled")
                block.after_idle(block._fit_height)
                widget.configure(state="normal")
                start = pos + " +1line linestart"
                widget.replace(start, start + " lineend", summary, "chat_source")
        finally:
            widget.configure(state="disabled")

    @staticmethod
    def key(index: int, role: str, content: str) -> str:
        if role == "analysis":
            return "analysis:" + content
        return hashlib.sha256(f"{index}:{role}:{content.strip()}".encode()).hexdigest()

    def _link(self, label: str, callback) -> None:
        tag = f"chat_link_{len(self.links)}"
        self.links.append(tag)
        self.widget.tag_configure(tag, foreground=COLORS["subtle"],
                                  font=("Microsoft YaHei UI", 9), spacing1=6, spacing3=8)
        click = self.widget.tag_bind(tag, "<Button-1>", lambda _event: callback())
        enter = self.widget.tag_bind(tag, "<Enter>", lambda _event: (
            self.widget.configure(cursor="hand2"),
            self.widget.tag_configure(tag, foreground=COLORS["text"])))
        leave = self.widget.tag_bind(tag, "<Leave>", lambda _event: (
            self.widget.configure(cursor="xterm"),
            self.widget.tag_configure(tag, foreground=COLORS["subtle"])))
        self._link_commands.extend((click, enter, leave))
        self.widget.insert("end", label, tag)

    def _copy(self, content: str) -> None:
        self.widget.clipboard_clear()
        self.widget.clipboard_append(content)

    def reading_anchor(self, key: str | None = None):
        """保存可识别的消息或折叠链接及其屏幕坐标。"""
        widget = self.widget
        if key in self._anchors:
            info = widget.dlineinfo(self._anchors[key])
            if info:
                return key, 0, 0, info[1]
        top = widget.index("@0,0")
        # 外层异常详情切换可能增删整条系统记录，以稳定的消息身份定位。
        messages = [(k, pos) for k, pos in self._anchors.items() if k.startswith("message:")]
        for k, pos in messages:
            if widget.compare(pos, ">=", top):
                info = widget.dlineinfo(pos)
                if info:
                    return k, 0, 0, info[1]
                break
        for k, pos in reversed(messages):
            if widget.compare(pos, "<=", top):
                line, col = map(int, top.split("."))
                info = widget.dlineinfo(top)
                return k, line - int(pos.split(".")[0]), col, info[1] if info else 0
        return None

    def restore_anchor(self, anchor) -> None:
        if anchor is None or anchor[0] not in self._anchors:
            return
        key, offset, column, screen_y = anchor
        widget = self.widget
        pos = self._anchors[key]
        if offset or column:
            pos = widget.index(f"{int(pos.split('.')[0]) + offset}.{column}")
        widget.update_idletasks()  # 内嵌气泡尺寸确定后再恢复像素偏移。
        widget.yview(pos)
        info = widget.dlineinfo(pos)
        if info:
            widget.yview_scroll(info[1] - screen_y, "pixels")

    def toggle(self, key: str) -> None:
        anchor = self.reading_anchor(key)
        if key in self._default_details:
            self._collapsed_details.symmetric_difference_update({key})
        else:
            self.expanded.symmetric_difference_update({key})
        self.render(self.entries, details_open=self.details_open, preserve_scroll=True)
        self.restore_anchor(anchor)

    def expand_all(self) -> None:
        anchor = self.reading_anchor()
        self._collapsed_details.clear()
        self.expanded = {
            self.key(i, role, content) for i, (role, content) in enumerate(self.entries)
        }
        self.render(self.entries, details_open=self.details_open, preserve_scroll=True)
        self.restore_anchor(anchor)

    def collapse_all(self) -> None:
        anchor = self.reading_anchor()
        self.expanded.clear()
        self.render(self.entries, details_open=False, preserve_scroll=True)
        self.restore_anchor(anchor)

    def _more_analysis(self, key: str) -> None:
        self.analysis_limits[key] = self.analysis_limits.get(key, 50) + 50
        self.render(self.entries, details_open=self.details_open, preserve_scroll=True)

    def _render_analysis(self, key: str) -> None:
        section = self.analysis_sections.get(key)
        if section is None:
            return
        if self.open_analysis is not None:
            self._anchors["analysis:" + key] = self.widget.index("end-1c")
            self.widget.insert("end", "调查中" if key == self.running_section else "调查过程",
                               "chat_muted")
            self.widget.insert("end", "\n")
            self._reply_bubble(self.summary(key), section=key)
            self.widget.insert("end", "\n", "chat_gap")
            return
        if not section.steps:
            return
        widget = self.widget
        opened = "analysis:" + key in self.expanded
        self._anchors["analysis:" + key] = widget.index("end-1c")
        if not self.detail_view:
            self._link(
                f"{'收起' if opened else '展开'}分析过程 · {len(section.steps)} 项",
                lambda: self.toggle("analysis:" + key),
            )
            widget.insert("end", "\n", "chat_muted")
            latest = section.steps[-1]
            widget.insert("end", f"{latest.source} · {latest.summary}\n", "chat_muted")
            if not opened:
                widget.insert("end", "\n", "chat_gap")
                return
            widget.insert("end", "公开说明与操作记录；模型判断不等于验证结论。\n", "chat_muted")
        limit = self.analysis_limits.get(key, 50)
        if len(section.steps) > limit:
            self._link(f"查看更早记录（剩余 {len(section.steps) - limit} 项）",
                       lambda: self._more_analysis(key))
            widget.insert("end", "\n")
        groups = []
        # 先按完整记录确定分组身份，再限制本次渲染数量。
        # 否则超过50项后每次刷新都会改变锚点，用户展开的详情会自行收起。
        for step in section.steps:
            if groups and step.group and groups[-1][-1].group == step.group:
                groups[-1].append(step)
            else:
                groups.append([step])
        skipped = max(0, len(section.steps) - limit)
        for steps in groups:
            first = steps[0]
            count = len(steps)
            steps = steps[skipped:]
            skipped = max(0, skipped - count)
            if not steps:
                continue
            detail_key = "analysis-step:" + first.key
            detail_open = detail_key in self.expanded
            label = f"{first.time.astimezone():%H:%M:%S} · {first.source} · {first.title}"
            if len(steps) > 1:
                label += f" · {len(steps)} 次"
            warning = any(step.failed for step in steps)
            widget.insert("end", label + "  ",
                          ("chat_bold", "chat_warning") if warning else "chat_bold")
            self._anchors[detail_key] = widget.index("end-1c")
            self._link("收起" if detail_open else "详情", lambda k=detail_key: self.toggle(k))
            widget.insert("end", "\n")
            if not detail_open or not self.detail_view:
                widget.insert("end", steps[-1].summary + "\n", "assistant_message")
            if detail_open:
                for item in steps:
                    insert_markdown(widget, item.detail or item.summary, "metadata")
                    widget.insert("end", "\n", "chat_gap")
        widget.insert("end", "\n", "chat_gap")

    def render(self, entries: list[tuple[str, str]], *, details_open: bool = False,
               preserve_scroll: bool = False) -> None:
        if self._latest_job is not None:
            self.widget.after_cancel(self._latest_job)
            self._latest_job = None
        position = self.widget.yview()[0]
        follow_bottom = self.widget.yview()[1] >= .98
        self.entries = entries
        if not details_open:
            self._collapsed_details.clear()
        self.details_open = details_open
        self.analysis_limits = {key: value for key, value in self.analysis_limits.items()
                                if key in self.analysis_sections}
        valid = {self.key(i, role, content) for i, (role, content) in enumerate(entries)}
        valid.update("analysis-step:" + step.key for section in self.analysis_sections.values()
                     for step in section.steps)
        self.expanded.intersection_update(valid)
        widget = self.widget
        widget.configure(state="normal")
        for block in self.blocks:
            block.destroy()
        self.blocks.clear()
        self.analysis_bubbles.clear()
        for tile in self.image_tiles:
            tile.destroy()
        self.image_tiles.clear()
        widget.delete("1.0", "end")
        for tag in self.links:
            widget.tag_delete(tag)
        self.links.clear()
        for command in self._link_commands:
            widget.deletecommand(command)
        self._link_commands.clear()
        for tag in ("assistant_message", "system_message", "metadata"):
            widget.tag_configure(tag, spacing1=1, spacing3=3)
        names = {"user": "你", "assistant": "ACE", "system": "系统记录",
                 "metadata": "调查与验证详情"}
        latest = max((i for i, item in enumerate(entries) if item[0] == "assistant"), default=-1)
        widget.mark_set("latest_reply", "1.0")
        self._anchors.clear()
        self._default_details.clear()
        ordinals = {"user": 0, "assistant": 0}
        for index, (role, content) in enumerate(entries):
            if role == "analysis":
                self._render_analysis(content)
                continue
            content = content.strip()
            key = self.key(index, role, content)
            long = role != "user" and (len(content) > 1200 or len(content.splitlines()) > 24)
            foldable = long or role in {"metadata", "system"}
            if details_open and role in {"metadata", "system"}:
                self._default_details.add(key)
            opened = (key in self.expanded or key in self._default_details) and (
                key not in self._collapsed_details
            )
            if role in ordinals:
                self._anchors[f"message:{role}:{ordinals[role]}"] = widget.index("end-1c")
                ordinals[role] += 1
            if index == latest:
                widget.mark_set("latest_reply", widget.index("end-1c"))
                widget.mark_gravity("latest_reply", "left")
            widget.insert("end", names[role], f"{role}_name" if role != "metadata"
                          else "chat_heading")
            widget.insert("end", "    ")
            if index < len(self.timestamps):
                widget.insert("end", self.timestamps[index] + "    ", "chat_muted")
            if foldable:
                self._anchors[key] = widget.index("end-1c")
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
                if index in self.attachments:
                    start = widget.index("end-1c")
                    for number, item in numbered_attachments(self.attachments[index]):
                        tile = AttachmentTile(widget, item, number)
                        self.image_tiles.append(tile)
                        widget.window_create("end", window=tile, padx=2)
                        widget.insert("end", " ", "user_block")
                    widget.insert("end", "\n", "user_block")
                    widget.tag_add("user_block", start, "end-1c")
            elif role == "assistant":
                self._reply_bubble(shown)
            else:
                insert_markdown(widget, shown, base,
                                lambda code: self._insert_block(code, code=True))
            widget.insert("end", "\n\n", "chat_gap")
        widget.tag_raise("sel")
        self.resize()
        widget.configure(state="disabled")
        self._initial_scroll = None
        if preserve_scroll:
            if follow_bottom:
                widget.see("end")
            else:
                widget.yview_moveto(position)
        else:
            if latest >= 0:
                widget.yview("latest_reply")
                self._initial_scroll = "latest_reply"
                if widget.winfo_viewable():
                    self._latest_job = widget.after_idle(self._scroll_after_layout)
            else:
                widget.see("end")
