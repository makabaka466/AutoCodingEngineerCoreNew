"""桌面工作区共用的中性色、线性图标与轻量提示，不依赖额外 UI 框架。"""

import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

COLORS = {
    "window": "#FFFFFF", "ambient": "#FFFFFF", "sidebar": "#F7F7F8",
    "surface": "#FFFFFF", "surface_subtle": "#F7F7F8", "glass": "#FFFFFF",
    "glass_floating": "#FFFFFF", "panel": "#F0F1F3", "panel_hover": "#E9EAED",
    "input": "#FFFFFF", "border": "#E7E8EB", "border_soft": "#E7E8EB",
    "border_strong": "#D4D6DB", "shadow": "#F0F1F3", "shadow_soft": "#F5F5F6",
    "text": "#24262B", "muted": "#696D76", "subtle": "#90949C",
    "accent": "#303844", "accent_hover": "#444E5D", "accent_soft": "#F0F3F7",
    "accent_border": "#D7DDE5", "progress_accent": "#64748B",
    "progress_accent_soft": "#FFFFFF", "progress_border": "#FFFFFF",
    "user": "#EEF1F5", "success": "#34704B", "success_soft": "#F1F6F2",
    "warning": "#996521", "warning_soft": "#FAF7F0",
    "danger": "#B04747", "danger_soft": "#FAF1F1",
}


class Tooltip:
    """鼠标或键盘焦点停留时显示；销毁控件时一并释放提示窗口。"""

    def __init__(self, widget: tk.Widget, text: str | tk.StringVar):
        self.widget, self.text = widget, text
        self.window = None
        self.timer = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<FocusIn>", self.schedule, add="+")
        for event in ("<Leave>", "<FocusOut>", "<ButtonPress>", "<Destroy>"):
            widget.bind(event, self.hide, add="+")

    def schedule(self, _event=None):
        self.hide()
        self.timer = self.widget.after(550, self.show)

    def show(self):
        self.timer = None
        if not self.widget.winfo_viewable():
            return
        text = self.text.get() if isinstance(self.text, tk.StringVar) else self.text
        if not text:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        tk.Label(self.window, text=text, bg="#30343B", fg="white", padx=9, pady=5,
                 font=("Microsoft YaHei UI", 9), wraplength=420).pack()
        self.window.geometry(f"+{self.widget.winfo_rootx()+8}"
                             f"+{self.widget.winfo_rooty()+self.widget.winfo_height()+5}")

    def hide(self, _event=None):
        if self.timer:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.window:
            self.window.destroy()
            self.window = None


class IconButton(tk.Button):
    """统一 20px 单色线性图标，保留 Tk 原生禁用、焦点和命令行为。"""

    def __init__(self, parent, icon, label, command):
        image = Image.new("RGBA", (80, 80))
        draw = ImageDraw.Draw(image)
        color = COLORS["muted"]
        paths = {
            "chat": [[(4, 4), (16, 4), (16, 14), (8, 14), (4, 17), (4, 4)]],
            "code": [[(7, 5), (2, 10), (7, 15)], [(13, 5), (18, 10), (13, 15)]],
            "incident": [[(10, 2), (18, 17), (2, 17), (10, 2)],
                         [(10, 7), (10, 11)], [(10, 13), (10, 14)]],
            "book": [[(2, 3), (8, 3), (10, 5), (12, 3), (18, 3), (18, 16),
                      (12, 16), (10, 18), (8, 16), (2, 16), (2, 3)], [(10, 5), (10, 18)]],
            "settings": [[(3, 5), (17, 5)], [(3, 10), (17, 10)], [(3, 15), (17, 15)],
                         [(7, 3), (7, 7)], [(13, 8), (13, 12)], [(8, 13), (8, 17)]],
            "log": [[(4, 2), (13, 2), (17, 6), (17, 18), (4, 18), (4, 2)],
                    [(7, 8), (14, 8)], [(7, 12), (14, 12)]],
            "panel": [[(2, 3), (18, 3), (18, 17), (2, 17), (2, 3)], [(7, 3), (7, 17)]],
            "chart": [[(3, 3), (3, 17), (18, 17)], [(7, 13), (7, 10)],
                      [(11, 13), (11, 7)], [(15, 13), (15, 4)]],
            "plus": [[(10, 3), (10, 17)], [(3, 10), (17, 10)]],
            "search": [[(13, 13), (18, 18)]],
            "more": [[(4, 9), (4, 11)], [(10, 9), (10, 11)], [(16, 9), (16, 11)]],
        }
        for points in paths[icon]:
            draw.line([(x*4, y*4) for x, y in points], fill=color, width=6, joint="curve")
        if icon == "search":
            draw.ellipse((8, 8, 56, 56), outline=color, width=6)
        self._icon = ImageTk.PhotoImage(image.resize((20, 20), Image.Resampling.LANCZOS),
                                      master=parent)
        super().__init__(parent, image=self._icon, command=command, width=36, height=36,
                         bg=parent.cget("bg"), activebackground=COLORS["panel_hover"],
                         relief="flat", bd=0, highlightthickness=0, cursor="hand2", takefocus=1)
        self._surface = parent.cget("bg")
        self._selected = False
        self.bind("<Enter>", lambda _: self.configure(bg=COLORS["panel_hover"]))
        self.bind("<Leave>", lambda _: self.set_selected(self._selected))
        self.bind("<Return>", lambda _: self.invoke())
        self.tooltip = Tooltip(self, label)

    def set_selected(self, selected):
        self._selected = selected
        self.configure(bg=COLORS["panel_hover"] if selected else self._surface)

    @property
    def selected(self):
        return self._selected

    def set_enabled(self, enabled):
        self.configure(state="normal" if enabled else "disabled")


class RoundedTextBlock(tk.Canvas):
    """正文中的可选择文本块；用户气泡与代码块共用，不参与消息存储。"""

    def __init__(self, parent: tk.Text, content: str, *, code: bool = False):
        self.parent_text, self.code = parent, code
        self.fill = "#F6F7F9" if code else COLORS["user"]
        super().__init__(parent, bg=COLORS["surface"], bd=0, highlightthickness=0, height=60)
        self.text = tk.Text(self, bg=self.fill, fg=COLORS["text"], relief="flat", bd=0,
                            highlightthickness=0, wrap="none" if code else "word",
                            font=("Consolas", 10) if code else ("Microsoft YaHei UI", 10),
                            spacing1=2, spacing3=3, padx=0, pady=0, height=1,
                            exportselection=False, cursor="xterm")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")
        self.text_window = self.create_window(14, 40 if code else 14, anchor="nw", window=self.text)
        self.scroll = None
        if code:
            self.copy = tk.Button(self, text="复制代码", bg=self.fill, fg=COLORS["muted"],
                                  relief="flat", bd=0, font=("Microsoft YaHei UI", 8),
                                  command=lambda: (parent.clipboard_clear(),
                                                   parent.clipboard_append(content)))
            self.copy_window = self.create_window(14, 6, anchor="nw", window=self.copy)
            self.scroll = tk.Scrollbar(self, orient="horizontal", command=self.text.xview)
            self.text.configure(xscrollcommand=self.scroll.set)
            self.scroll_window = self.create_window(14, 60, anchor="nw", window=self.scroll)
        self.text.bind("<MouseWheel>", self._scroll_chat)
        self.bind("<Configure>", self._layout)

    def _scroll_chat(self, event):
        self.parent_text.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def _layout(self, _event=None):
        width = max(100, self.winfo_width())
        self.itemconfigure(self.text_window, width=width - 28)
        self.after_idle(self._fit_height)

    def _fit_height(self):
        if not self.winfo_exists():
            return
        rows = self.text.count("1.0", "end", "displaylines")
        lines = max(1, rows[0] if rows else 1)
        # Tk Text 按字体计算请求高度，无需猜测系统 DPI。
        self.text.configure(height=lines)
        text_height = self.text.winfo_reqheight()
        height = text_height + (66 if self.code else 28)
        if int(self.cget("height")) != height:
            self.configure(height=height)
        self.itemconfigure(self.text_window, height=text_height)
        width = max(100, self.winfo_width())
        self.delete("surface")
        self.create_polygon(14, 0, width-14, 0, width, 0, width, 14,
                            width, height-14, width, height, width-14, height,
                            14, height, 0, height, 0, height-14, 0, 14, 0, 0,
                            smooth=True, fill=self.fill, outline="", tags="surface")
        self.tag_lower("surface")
        if self.scroll:
            self.coords(self.scroll_window, 14, height-22)
            self.itemconfigure(self.scroll_window, width=width-28)
