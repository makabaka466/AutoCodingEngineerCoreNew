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

    def prepare_for_send(self, attachment: MessageAttachment) -> MessageAttachment:
        """Revalidate an owned screenshot and refresh harmless stale file metadata.

        Clipboard and image-provider implementations can finish updating file metadata after the
        initial capture. The Engine intentionally keeps strict size validation, so the desktop
        boundary reseals the current valid PNG immediately before handing it to the Engine.
        """

        try:
            from PIL import Image, UnidentifiedImageError
        except ImportError as exc:
            raise IncidentAttachmentError(
                "图片粘贴组件未安装，请重新运行启动脚本安装 Pillow。"
            ) from exc

        root = self.root.resolve()
        try:
            path = Path(attachment.path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise IncidentAttachmentError(
                f"异常截图已不可用，请移除后重新粘贴：{attachment.name}"
            ) from exc
        expected = (root / attachment.id / "incident-screenshot.png").resolve()
        if path != expected or not path.is_relative_to(root) or not path.is_file():
            raise IncidentAttachmentError(
                f"异常截图不在 ACE 的隔离附件目录中：{attachment.name}"
            )
        size = path.stat().st_size
        if size < 1 or size > MAX_ATTACHMENT_BYTES:
            raise IncidentAttachmentError(
                f"异常截图为空或超过 10 MiB，请重新粘贴：{attachment.name}"
            )
        try:
            with Image.open(path) as image:
                width, height = image.size
                if width < 1 or height < 1 or width * height > MAX_ATTACHMENT_PIXELS:
                    raise IncidentAttachmentError(
                        f"异常截图尺寸无效或过大：{attachment.name}"
                    )
                if image.format != "PNG":
                    raise IncidentAttachmentError(
                        f"异常截图内容不是有效 PNG：{attachment.name}"
                    )
                image.verify()
        except IncidentAttachmentError:
            raise
        except (OSError, UnidentifiedImageError) as exc:
            raise IncidentAttachmentError(
                f"异常截图已损坏，请移除后重新粘贴：{attachment.name}"
            ) from exc
        return attachment.model_copy(
            update={
                "path": str(path),
                "name": path.name,
                "media_type": "image/png",
                "size_bytes": size,
            }
        )
