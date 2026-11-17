"""异常截图的缩略图和桌面大图预览；只读用户消息关联的本地附件。"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from tkinter import messagebox

from PIL import Image, ImageTk

from autocoding_agent.core.models import MessageAttachment
from autocoding_agent.incident_attachments import MAX_ATTACHMENT_BYTES, MAX_ATTACHMENT_PIXELS
from autocoding_agent.interfaces.desktop_theme import COLORS, Tooltip

TILE_WIDTH = 88
TILE_HEIGHT = 64


def _read_image(path: Path, size: tuple[int, int] | None = None) -> Image.Image:
    if not 0 < path.stat().st_size <= MAX_ATTACHMENT_BYTES:
        raise ValueError("图片为空或超过大小限制。")
    with Image.open(path, formats=("PNG", "JPEG", "WEBP", "GIF")) as image:
        if image.width * image.height > MAX_ATTACHMENT_PIXELS:
            raise ValueError("图片尺寸过大。")
        if size:
            image.thumbnail(size, Image.Resampling.LANCZOS)
        return image.convert("RGBA")


@lru_cache(maxsize=128)
def _thumbnail(path: Path, modified: int) -> Image.Image:
    # 文件变动自动失效；只缓存小图，避免实时会话重绘反复解码原图。
    return _read_image(path, (TILE_WIDTH - 4, TILE_HEIGHT - 4))


class AttachmentTile(tk.Frame):
    """草稿和历史共用；只有草稿提供逐张移除操作。"""

    def __init__(self, parent, item: MessageAttachment, number: int,
                 remove: Callable[[str], None] | None = None) -> None:
        super().__init__(parent, width=TILE_WIDTH, height=TILE_HEIGHT,
                         bg=COLORS["panel"], highlightthickness=1,
                         highlightbackground=COLORS["border"])
        path = Path(item.path)
        self.photo = None
        try:
            self.photo = ImageTk.PhotoImage(_thumbnail(path, path.stat().st_mtime_ns), master=self)
        except (OSError, ValueError, Image.DecompressionBombError):
            pass
        button = tk.Button(self, image=self.photo, text="" if self.photo else "图片不可用",
                           bg=COLORS["surface_subtle"], fg=COLORS["muted"], bd=0,
                           relief="flat", highlightthickness=0, cursor="hand2",
                           command=lambda: self._open(path, number))
        button.place(x=0, y=0, relwidth=1, relheight=1)
        Tooltip(button, f"查看图{number}")
        caption = tk.Label(self, text=f"图{number}", bg=COLORS["surface"], fg=COLORS["muted"],
                           font=("Microsoft YaHei UI", 8), cursor="hand2")
        caption.place(x=1, rely=1, y=-1, anchor="sw")
        caption.bind("<Button-1>", lambda _: button.invoke())
        if remove:
            close = tk.Button(self, text="×", command=lambda: remove(item.id),
                              font=("Segoe UI", 10, "bold"), relief="flat", bd=0,
                              bg=COLORS["surface"], fg=COLORS["text"], cursor="hand2")
            close.place(relx=1, x=-1, y=1, anchor="ne", width=20, height=20)
            Tooltip(close, f"移除图{number}")

    def _open(self, path: Path, number: int) -> None:
        owner = self.winfo_toplevel()
        try:
            image = _read_image(path)
        except (OSError, ValueError, Image.DecompressionBombError):
            messagebox.showinfo("图片不可用", "图片文件已移动、损坏或无法读取。", parent=owner)
            return
        previous = owner.children.get("ace_image_preview")
        if previous:
            previous.destroy()
        ImageViewer(owner, image, number)


class ImageViewer(tk.Toplevel):
    """初始适应窗口。原始大小模式只绘制当前可见区域，拖动即可查看其余部分。"""

    def __init__(self, owner, image: Image.Image, number: int) -> None:
        super().__init__(owner, name="ace_image_preview")
        self.source = image
        self.fit = True
        self.offset = (0, 0)
        self.drag = (0, 0)
        self.title(f"图{number} · 截图预览")
        self.transient(owner)
        width = min(max(320, image.width), int(self.winfo_screenwidth() * .85))
        height = min(max(180, image.height), int(self.winfo_screenheight() * .80))
        self.geometry(f"{width}x{height + 38}")
        self.toggle = tk.Button(self, text="查看原始大小", command=self._toggle, relief="flat")
        self.toggle.pack(side="bottom", pady=3)
        self.canvas = tk.Canvas(self, bg=COLORS["surface_subtle"], highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._paint)
        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._pan)
        self.bind("<Escape>", lambda _: self.destroy())
        self.photo = None
        self.image_id = self.canvas.create_image(0, 0)
        self.focus_set()

    def destroy(self) -> None:
        super().destroy()
        self.source.close()
        self.photo = None

    def _toggle(self) -> None:
        self.fit = not self.fit
        self.toggle.configure(text="查看原始大小" if self.fit else "适应窗口")
        self.canvas.configure(cursor="" if self.fit else "fleur")
        self._paint()

    def _start_drag(self, event) -> None:
        self.drag = (event.x + self.offset[0], event.y + self.offset[1])

    def _pan(self, event) -> None:
        if not self.fit:
            self.offset = (self.drag[0] - event.x, self.drag[1] - event.y)
            self._paint()

    def _paint(self, _event=None) -> None:
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if self.fit:
            scale = min(width / self.source.width, height / self.source.height, 1.0)
            size = (max(1, int(self.source.width * scale)), max(1, int(self.source.height * scale)))
            view = self.source.resize(size, Image.Resampling.LANCZOS, reducing_gap=2.0)
        else:
            x = max(0, min(self.offset[0], self.source.width - width))
            y = max(0, min(self.offset[1], self.source.height - height))
            self.offset = (x, y)
            view = self.source.crop((x, y, min(self.source.width, x + width),
                                     min(self.source.height, y + height)))
        self.photo = ImageTk.PhotoImage(view, master=self)
        self.canvas.itemconfigure(self.image_id, image=self.photo)
        self.canvas.coords(self.image_id, width // 2, height // 2)
