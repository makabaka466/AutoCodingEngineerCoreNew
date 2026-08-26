"""Clipboard image capture and isolated local storage for incident evidence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from autocoding_agent.config import Settings, get_settings
from autocoding_agent.core.models import MessageAttachment

if TYPE_CHECKING:
    from PIL.Image import Image

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_PIXELS = 40_000_000
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class IncidentAttachmentError(ValueError):
    """A safe clipboard or image validation error suitable for the desktop UI."""


class IncidentAttachmentStore:
    """Store every pasted image in its own directory outside the target repository."""

    def __init__(self, settings: Settings | None = None, root: Path | None = None) -> None:
        configured = settings or get_settings()
        self.root = (root or configured.data_dir / "attachments" / "incident").resolve()

    def capture_clipboard_image(self) -> MessageAttachment | None:
        """Return None for ordinary text clipboard content and an attachment for an image."""

        try:
            from PIL import Image, ImageGrab, UnidentifiedImageError
        except ImportError as exc:
            raise IncidentAttachmentError(
                "图片粘贴组件未安装，请重新运行启动脚本安装 Pillow。"
            ) from exc

        try:
            payload = ImageGrab.grabclipboard()
        except (OSError, RuntimeError) as exc:
            raise IncidentAttachmentError(f"无法读取系统剪贴板图片：{exc}") from exc

        if isinstance(payload, Image.Image):
            try:
                return self.save_image(payload)
            finally:
                payload.close()
        if isinstance(payload, list):
            for raw_path in payload:
                source = Path(raw_path)
                if source.suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES:
                    continue
                try:
                    with Image.open(source) as image:
                        image.load()
                        return self.save_image(image)
                except (OSError, UnidentifiedImageError) as exc:
                    raise IncidentAttachmentError(f"无法读取粘贴的图片文件：{exc}") from exc
        return None

    def save_image(self, image: Image) -> MessageAttachment:
        """Normalize a Pillow image to PNG and enforce deterministic host limits."""

        width, height = image.size
        if width < 1 or height < 1 or width * height > MAX_ATTACHMENT_PIXELS:
            raise IncidentAttachmentError("图片尺寸无效或超过 4000 万像素限制。")
        attachment_id = str(uuid4())
        directory = self.root / attachment_id
        directory.mkdir(parents=True, exist_ok=False)
        target = directory / "incident-screenshot.png"
        temporary = directory / ".incident-screenshot.png.tmp"
        try:
            normalized = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            try:
                normalized.save(temporary, format="PNG", optimize=True)
            finally:
                normalized.close()
            size = temporary.stat().st_size
            if size < 1 or size > MAX_ATTACHMENT_BYTES:
                raise IncidentAttachmentError("图片文件超过 10 MiB 限制。")
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass
            raise
        return MessageAttachment(
            id=attachment_id,
            path=str(target.resolve()),
            name=target.name,
            media_type="image/png",
            size_bytes=size,
        )
