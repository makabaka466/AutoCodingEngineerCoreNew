"""在可见的历史行旁绘制运行状态，文字列表仍使用原生选择和滚动。"""

import tkinter as tk

from autocoding_agent.interfaces.desktop_theme import COLORS

_STATE_ICONS = {
    "queued": ("◷", "排队中"),
    "waiting_input": ("?", "待你回答"),
    "waiting_modify_approval": ("◇", "等待审批"),
    "waiting_verify_approval": ("◇", "等待审批"),
    "waiting_approval": ("◇", "等待审批"),
    "paused": ("Ⅱ", "已暂停"),
    "recovery_required": ("Ⅱ", "等待恢复"),
    "cancelled": ("–", "已取消"),
    "failed": ("!", "执行失败"),
}


class HistoryIndicators(tk.Canvas):
    def __init__(self, parent, listing):
        super().__init__(parent, width=20, height=1, bd=0, highlightthickness=0,
                         bg=COLORS["sidebar"])
        self.listing = listing
        self.keys = []
        self.states = {}
        self.hint = tk.StringVar(self)
        self._layout = None
        self._running = False
        self._angle = 0
        self.bind("<Map>", lambda _: self.after_idle(self.paint))
        self.bind("<Motion>", self._hover)
        self.bind("<Button-1>", self._select)
        self.bind("<MouseWheel>", lambda e: listing.event_generate("<MouseWheel>", delta=e.delta))

    def _select(self, event):
        if not self.keys:
            return
        row = self.listing.nearest(event.y)
        box = self.listing.bbox(row)
        if box and event.y <= box[1] + box[3]:
            self.listing.selection_clear(0, "end")
            self.listing.selection_set(row)
            self.listing.event_generate("<<ListboxSelect>>")

    def _hover(self, event):
        if self.keys:
            row = min(len(self.keys) - 1, self.listing.nearest(event.y))
            state = self.states.get(self.keys[row])
            hint = "正在执行" if state == "running" else _STATE_ICONS.get(state, ("", ""))[1]
            self.hint.set(hint)

    def paint(self):
        if not self.winfo_ismapped():
            return
        listing = self.listing
        first, last = listing.nearest(0), listing.nearest(listing.winfo_height())
        visible = self.keys[first:last + 1]
        bounds = listing.bbox(first) if visible else None
        layout = (first, bounds, listing.winfo_height(),
                  tuple(self.states.get(key) for key in visible))
        if layout != self._layout:
            self._layout = layout
            self.delete("all")
            self._running = False
            for i, key in enumerate(visible, first):
                box = listing.bbox(i)
                state = self.states.get(key)
                if not box:
                    continue
                y = box[1] + box[3] / 2
                if state == "running":
                    self._running = True
                    self.create_arc(4, y - 5, 14, y + 5, start=self._angle, extent=255, width=2,
                                    outline="#74879D", style="arc", tags="running")
                elif state in _STATE_ICONS:
                    self.create_text(9, y, text=_STATE_ICONS[state][0], font=("Segoe UI", 10),
                                     fill="#9C4945" if state == "failed" else COLORS["muted"])

    def animate(self):
        if self._running and self.winfo_ismapped():
            self._angle = (self._angle - 25) % 360
            self.itemconfigure("running", start=self._angle)
