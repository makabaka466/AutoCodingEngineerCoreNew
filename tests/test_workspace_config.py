from __future__ import annotations

from pathlib import Path

import pytest

from autocoding_agent.workspace_config import (
    WorkspaceConfigError,
    WorkspaceConfigService,
    WorkspaceConfigStore,
)


def test_workspace_configuration_is_persisted_and_reloaded(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = WorkspaceConfigStore(tmp_path / "state")
    service = WorkspaceConfigService(store=store)

    saved = service.save(workspace)
    loaded = WorkspaceConfigService(store=store).inspect()

    assert saved.configured is True
    assert loaded.configured is True
    assert loaded.config is not None
    assert loaded.config.path == str(workspace.resolve())
    assert store.path == tmp_path / "state" / "workspace" / "project.json"


def test_workspace_configuration_reports_a_saved_path_that_became_unavailable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = WorkspaceConfigStore(tmp_path / "state")
    store.save(workspace)
    workspace.rmdir()

    state = store.load()

    assert state.config is not None
    assert state.available is False
    assert state.configured is False


def test_workspace_configuration_rejects_missing_or_file_paths(tmp_path: Path) -> None:
    store = WorkspaceConfigStore(tmp_path / "state")
    file_path = tmp_path / "not-a-directory.txt"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(WorkspaceConfigError, match="请先选择"):
        store.save("")
    with pytest.raises(WorkspaceConfigError, match="必须是一个可访问的目录"):
        store.save(file_path)
    with pytest.raises(WorkspaceConfigError, match="不存在或无法访问"):
        store.save(tmp_path / "missing")
