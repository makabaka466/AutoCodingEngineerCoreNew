from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageGrab

import autocoding_agent.incident_attachments as attachment_module
from autocoding_agent.incident_attachments import (
    IncidentAttachmentError,
    IncidentAttachmentStore,
)


def test_incident_image_is_normalized_into_an_isolated_png_directory(
    tmp_path: Path,
) -> None:
    store = IncidentAttachmentStore(root=tmp_path / "attachments")
    image = Image.new("RGB", (32, 20), color=(210, 30, 50))
    try:
        attachment = store.save_image(image)
    finally:
        image.close()

    path = Path(attachment.path)
    assert path.is_file()
    assert path.name == "incident-screenshot.png"
    assert path.parent.name == attachment.id
    assert path.parent.parent == store.root
    assert attachment.media_type == "image/png"
    assert attachment.size_bytes == path.stat().st_size
    with Image.open(path) as saved:
        assert saved.format == "PNG"
        assert saved.size == (32, 20)


def test_text_clipboard_is_left_for_tk_default_paste(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: None)

    attachment = IncidentAttachmentStore(
        root=tmp_path / "attachments"
    ).capture_clipboard_image()

    assert attachment is None
    assert not (tmp_path / "attachments").exists()


def test_oversized_normalized_image_is_rejected_without_partial_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "attachments"
    monkeypatch.setattr(attachment_module, "MAX_ATTACHMENT_BYTES", 1)
    image = Image.new("RGB", (8, 8), color=(0, 0, 0))
    try:
        with pytest.raises(IncidentAttachmentError, match="超过 10 MiB"):
            IncidentAttachmentStore(root=root).save_image(image)
    finally:
        image.close()

    assert list(root.glob("**/*")) == []


def test_prepare_for_send_refreshes_stale_size_after_valid_png_change(
    tmp_path: Path,
) -> None:
    store = IncidentAttachmentStore(root=tmp_path / "attachments")
    original = Image.new("RGB", (8, 8), color=(0, 0, 0))
    try:
        attachment = store.save_image(original)
    finally:
        original.close()

    replacement = Image.new("RGB", (48, 32), color=(20, 120, 220))
    try:
        replacement.save(attachment.path, format="PNG", optimize=False)
    finally:
        replacement.close()

    assert Path(attachment.path).stat().st_size != attachment.size_bytes
    prepared = store.prepare_for_send(attachment)

    assert prepared.path == attachment.path
    assert prepared.size_bytes == Path(attachment.path).stat().st_size


def test_prepare_for_send_rejects_invalid_replacement_content(tmp_path: Path) -> None:
    store = IncidentAttachmentStore(root=tmp_path / "attachments")
    image = Image.new("RGB", (8, 8), color=(0, 0, 0))
    try:
        attachment = store.save_image(image)
    finally:
        image.close()
    Path(attachment.path).write_bytes(b"not-a-real-image")

    with pytest.raises(IncidentAttachmentError, match="已损坏"):
        store.prepare_for_send(attachment)
