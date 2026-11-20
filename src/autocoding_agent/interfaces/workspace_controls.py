"""工作台的圆角浮层与细滚动条，使用 Tk 原生事件及滚动协议。"""

import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from typing import Callable

from autocoding_agent.interfaces.desktop_theme import COLORS


def rounded_points(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
) -> list[float]:
    """Return one reusable smooth-polygon path for native rounded surfaces."""

    return [
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


def _rounded(canvas, left, top, right, bottom, radius, **options):
    radius = max(0, min(radius, (right - left) / 2, (bottom - top) / 2))
    return canvas.create_polygon(rounded_points(left, top, right, bottom, radius),
                                 smooth=True, splinesteps=12, **options)


@dataclass
class _MenuItem:
    label: str
    command: Callable[[], object] | None
    enabled: bool
    selected: bool


class PopupMenu:
    """两处菜单共用键盘导航、可见范围和关闭行为。"""

    def __init__(self, anchor, *, width=None, above=False, return_focus=None):
        self.anchor = anchor
        self.width = width
        self.above = above
        self.return_focus = return_focus or anchor
        self.items: list[_MenuItem | None] = []
        self.window = None
        self.active = -1
        self.first = 0

    def add_item(self, label, command, *, enabled=True, selected=False):
        self.items.append(_MenuItem(label, command, bool(enabled), selected))

    def add_separator(self):
        self.items.append(None)

    def show(self):
        if not self.items:
            return
        anchor = self.anchor
        self.window = tk.Toplevel(anchor)
        win = self.window
        win.withdraw()
        win.overrideredirect(True)
        win.transient(anchor.winfo_toplevel())
        # 透明角仅用于窗口背景；白色菜单和浅色投影均在同一画布上。
        key_color = "#ff00fe"
        if win.tk.call("tk", "windowingsystem") == "win32":
            win.attributes("-transparentcolor", key_color)
        else:
            key_color = COLORS["window"]
        self.font = tkfont.Font(win, family="Microsoft YaHei UI", size=10)
        self.row_height = max(30, self.font.metrics("linespace") + 12)
        sx, sy = anchor.winfo_rootx(), anchor.winfo_rooty()
        sw, sh = anchor.winfo_screenwidth(), anchor.winfo_screenheight()
        width = min(sw, self.width or max(210, anchor.winfo_width()))
        above = max(0, sy - 8)
        below = max(0, sh - sy - anchor.winfo_height() - 8)
        desired = 16 + sum(self.row_height if item else 9 for item in self.items)
        up = self.above
        if (above if up else below) < desired and (above > below) != up:
            up = not up
        available = above if up else below
        self.capacity = min(len(self.items), max(1, (available - 16) // self.row_height))
        if desired <= available:
            self.capacity = len(self.items)
        height = (desired if self.capacity == len(self.items)
                  else 16 + self.capacity * self.row_height)
        self.active = next((i for i, item in enumerate(self.items) if item and item.selected), -1)
        self.first = max(0, self.active - self.capacity + 1)
        x = max(0, min(sx, sw - width))
        y = max(0, sy - height - 4) if up else sy + anchor.winfo_height() + 4
        win.geometry(f"{width}x{height}+{x}+{y}")
        self.canvas = tk.Canvas(win, bg=key_color, bd=0, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._paint)
        self.canvas.bind("<Motion>", self._point)
        self.canvas.bind("<ButtonRelease-1>", self._choose)
        win.bind("<ButtonPress-1>", self._outside)
        for sequence in ("<Down>", "<Up>", "<Home>", "<End>", "<Return>", "<space>"):
            win.bind(sequence, self._key)
        win.bind("<Escape>", lambda _: self.close())
        win.bind("<Tab>", lambda _: self.close())
        win.bind("<FocusOut>", self._lose_focus)
        win.bind("<MouseWheel>", self._wheel)
        win.deiconify()
        win.lift()
        win.update_idletasks()
        win.grab_set()
        self.canvas.focus_force()

    def _paint(self, _event=None):
        canvas = self.canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 5 or height < 5:
            return
        canvas.delete("all")
        _rounded(canvas, 1, 3, width - 1, height - 1, 12, fill="#e0e2e5", outline="")
        _rounded(canvas, 1, 1, width - 2, height - 3, 11, fill="white", outline="#d6d8dc")
        self.rows = []
        top = 7
        for idx in range(self.first, min(self.first + self.capacity, len(self.items))):
            item = self.items[idx]
            bottom = top + (self.row_height if item else 9)
            if item is None:
                canvas.create_line(13, top + 4, width - 13, top + 4, fill=COLORS["border"])
            else:
                if idx == self.active:
                    _rounded(canvas, 6, top, width - 7, bottom, 6,
                             fill=COLORS["panel"], outline="")
                color = COLORS["text"] if item.enabled else COLORS["subtle"]
                label = item.label
                if self.font.measure(label) > width - 52:
                    lo, hi = 0, len(label)
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        if self.font.measure(label[:mid] + "…") <= width - 52:
                            lo = mid
                        else:
                            hi = mid - 1
                    label = label[:lo] + "…"
                canvas.create_text(14, (top + bottom) / 2, anchor="w", text=label,
                                   font=self.font, fill=color)
                if item.selected:
                    canvas.create_text(width - 18, (top + bottom) / 2, text="✓", fill=color)
                self.rows.append((idx, top, bottom))
            top = bottom

    def _point(self, event):
        index = next((i for i, top, end in getattr(self, "rows", ())
                      if top <= event.y < end and self.items[i].enabled), -1)
        if index != self.active:
            self.active = index
            self._paint()

    def _choose(self, event=None):
        if event is not None:
            self._point(event)
        item = self.items[self.active] if self.active >= 0 else None
        if item and item.enabled:
            self.close()
            item.command()

    def _outside(self, event):
        if event.widget != self.canvas:
            self.close()

    def _lose_focus(self, event):
        if self.window and event.widget is self.window:
            self.window.after_idle(self._check_focus)

    def _check_focus(self):
        if self.window and self.window.focus_displayof() not in (self.window, self.canvas):
            self.close(restore_focus=False)

    def _key(self, event):
        if event.keysym in {"Return", "space"}:
            self._choose()
            return "break"
        enabled = [i for i, item in enumerate(self.items) if item and item.enabled]
        if not enabled:
            return "break"
        if event.keysym == "Home":
            self.active = enabled[0]
        elif event.keysym == "End":
            self.active = enabled[-1]
        elif event.keysym == "Down":
            self.active = next((i for i in enabled if i > self.active), enabled[0])
        else:
            self.active = next((i for i in reversed(enabled) if i < self.active), enabled[-1])
        self.first = min(self.active, max(self.first, self.active - self.capacity + 1))
        self._paint()
        return "break"

    def _wheel(self, event):
        shift = -1 if event.delta > 0 else 1
        self.first = min(len(self.items) - self.capacity, max(0, self.first + shift))
        self.active = -1
        self._paint()
        return "break"

    def close(self, *, restore_focus=True):
        if self.window:
            win, self.window = self.window, None
            win.destroy()
            if restore_focus and self.return_focus.winfo_viewable():
                self.return_focus.focus_force()
        return "break"


class SlimScrollbar(tk.Canvas):
    """保留滚轮、翻页和抓取范围；滑块长度按可见内容比例计算。"""

    def __init__(self, parent, *, command):
        super().__init__(parent, width=14, height=1, bg=parent.cget("bg"),
                         bd=0, highlightthickness=0)
        self.command = command
        self.first, self.last = 0.0, 1.0
        self.top, self.size = 0.0, 0.0
        self.drag = None
        self.hover = False
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", self._hover)
        self.bind("<Leave>", self._hover)
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<MouseWheel>", self._wheel)

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self._draw()

    def _draw(self, _event=None):
        self.delete("thumb")
        span = self.last - self.first
        height = self.winfo_height()
        if height <= 2 or span >= 1 or span <= 0:
            self.size = 0
            return
        self.size = min(height, max(26, round(height * span)))
        self.top = round((height - self.size) * self.first / (1 - span))
        hot = self.drag is not None or self.hover
        left = 3 if hot else 4
        _rounded(self, left, self.top, self.winfo_width() - left, self.top + self.size,
                 4, fill="#979da6" if hot else "#c8ccd2", outline="", tags="thumb")

    def _hover(self, event):
        self.hover = event.type == tk.EventType.Enter
        self._draw()

    def _press(self, event):
        if not self.size:
            return
        if self.top <= event.y <= self.top + self.size:
            self.drag = event.y - self.top
        else:
            self.command("scroll", -1 if event.y < self.top else 1, "pages")

    def _move(self, event):
        travel = self.winfo_height() - self.size
        if self.drag is not None and travel > 0:
            fraction = min(1.0, max(0.0, (event.y - self.drag) / travel))
            self.command("moveto", fraction * (1 - self.last + self.first))

    def _release(self, _event):
        self.drag = None
        self._draw()

    def _wheel(self, event):
        units = max(1, abs(event.delta) // 120) * (-1 if event.delta > 0 else 1)
        self.command("scroll", units * 3, "units")
        return "break"
