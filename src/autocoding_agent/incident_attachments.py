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

        images = self._capture_images(1, first_only=True)
        return images[0] if images else None

    def capture_clipboard_images(self, remaining: int = 5) -> list[MessageAttachment]:
        """按剪贴板顺序保存整批截图；超过当前消息余量时整批退回。"""
        return self._capture_images(remaining, first_only=False)

    def _capture_images(self, remaining: int, *, first_only: bool) -> list[MessageAttachment]:
        try:
            from PIL import Image, ImageGrab
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
                if remaining < 1:
                    raise IncidentAttachmentError("每条消息最多粘贴 5 张异常截图。")
                return [self.save_image(payload)]
            finally:
                payload.close()
        if not isinstance(payload, list):
            return []
        paths = [path for path in map(Path, payload) if path.suffix.casefold()
                 in SUPPORTED_IMAGE_SUFFIXES]
        if first_only:
            paths = paths[:1]
        return self.import_image_files(paths, remaining)

    def import_image_files(self, paths: list[Path], remaining: int) -> list[MessageAttachment]:
        """文件选择与剪贴板共用整批导入；任一文件失败时回收本批产生的副本。"""
        from PIL import Image

        if any(path.suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES for path in paths):
            raise IncidentAttachmentError("请选择 PNG、JPEG、WebP 或 GIF 图片。")
        if len(paths) > remaining:
            raise IncidentAttachmentError(
                f"本次有 {len(paths)} 张图片，这条消息还能添加 {remaining} 张。"
            )
        saved = []
        try:
            for source in paths:
                with Image.open(source) as image:
                    saved.append(self.save_image(image))
        except Exception as exc:
            for item in saved:
                target = Path(item.path)
                target.unlink(missing_ok=True)
                target.parent.rmdir()
            if isinstance(exc, OSError):
                raise IncidentAttachmentError(f"无法读取图片文件：{exc}") from exc
            raise
        return saved

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

    def discard_draft(self, item: MessageAttachment) -> None:
        """只清理本机草稿生成的隔离副本；上传源文件和已发送消息不走此入口。"""
        path = Path(item.path)
        if (path.parent.parent != self.root or path.parent.name != item.id
                or path.name != "incident-screenshot.png" or path.resolve() != path):
            return
        path.unlink(missing_ok=True)
        if path.parent.is_dir():
            path.parent.rmdir()

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
