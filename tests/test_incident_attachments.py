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


def test_clipboard_file_batch_preserves_order_and_rejects_partial_import(tmp_path, monkeypatch):
    store = IncidentAttachmentStore(root=tmp_path / "attachments")
    paths = [tmp_path / f"{i}.png" for i in (1, 2)]
    for i, path in enumerate(paths, 1):
        with Image.new("RGB", (i, i)) as image:
            image.save(path)
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: paths)
    with pytest.raises(IncidentAttachmentError, match="还能添加 1 张"):
        store.capture_clipboard_images(1)
    assert not store.root.exists()
    images = store.capture_clipboard_images(2)
    assert len({item.id for item in images}) == 2
    for i, item in enumerate(images, 1):
        with Image.open(item.path) as image:
            assert image.size == (i, i)
    before = set(store.root.iterdir())
    paths[1].write_bytes(b"invalid")
    with pytest.raises(IncidentAttachmentError):
        store.capture_clipboard_images(2)
    assert set(store.root.iterdir()) == before

@pytest.mark.parametrize("workflow", ["incident", "development"])
def test_mixed_attachments_reach_model_and_persist_per_message(tmp_path, workflow):
    from test_agent_flows import ScriptedRuntime, _app
    from test_incident_flow import ScriptedStructuredRuntime

    from autocoding_agent.adapters.json_incident_store import JsonIncidentStore
    from autocoding_agent.core.models import AgentDecision, AgentStatus
    from autocoding_agent.incident.engine import IncidentEngine
    from autocoding_agent.incident.models import IncidentDecision

    store = IncidentAttachmentStore(root=tmp_path / "attachments")
    log = tmp_path / "station.log"
    log.write_bytes("工位异常代码 E5123".encode("gbk"))
    document, = store.import_text_files([log], 5)
    with Image.new("RGB", (3, 4)) as image:
        pic = store.save_image(image)
    documents = [document, pic]
    workspace = tmp_path / "repo"
    workspace.mkdir()
    if workflow == "incident":
        runtime = ScriptedStructuredRuntime([
            IncidentDecision(status="needs_input", message="需要说明", question="哪个工位？")
            for _ in range(2)
        ])
        app = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "state"), None)
    else:
        decision = AgentDecision(status=AgentStatus.NEEDS_INPUT, message="需要说明")
        runtime = ScriptedRuntime(decision, decision)
        app = _app(tmp_path / "state", runtime)
    first = app.start(workspace, "调查工位", attachments=documents)
    log.unlink()
    app.send(first.session_id, "参考另一份日志", attachments=[document])
    assert "工位异常代码 E5123" in runtime.turns[0].user_message
    assert "图1:" in runtime.turns[0].user_message
    assert runtime.turns[0].attachment_paths == [pic.path]
    assert runtime.turns[1].attachment_paths == []
    assert "工位异常代码 E5123" in runtime.turns[1].user_message
    assert set(runtime.turns[0].additional_dirs) == {str(Path(i.path).parent) for i in documents}
    saved = app.get_session(first.session_id)
    assert [m.attachments for m in saved.messages if m.role == "user"] == [documents, [document]]


def test_file_batch_failure_discards_only_new_copies(tmp_path):
    store = IncidentAttachmentStore(root=tmp_path / "attachments")
    log = tmp_path / "run.log"
    log.write_text("正常", encoding="utf-8")
    binary = tmp_path / "bad.log"
    binary.write_bytes(b"a\x00b")
    with pytest.raises(IncidentAttachmentError, match="bad.log"):
        store.import_text_files([log, binary], 5)
    assert not list(store.root.iterdir())
    assert log.exists() and binary.exists()
