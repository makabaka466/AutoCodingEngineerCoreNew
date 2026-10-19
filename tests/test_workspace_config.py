from __future__ import annotations

from pathlib import Path

import pytest

from autocoding_agent.workspace_config import (
    WorkspaceConfigError,
    WorkspaceConfigService,
    WorkspaceConfigStore,
)
from autocoding_agent.workspace_knowledge import KnowledgeDomain, MarkdownKnowledgeService


def test_project_catalog_keeps_identity_history_and_empty_knowledge(tmp_path: Path) -> None:
    first, second, moved = (tmp_path / name for name in ("first", "second", "moved"))
    for path in (first, second, moved):
        path.mkdir()
    knowledge = MarkdownKnowledgeService(tmp_path / "ace")
    service = WorkspaceConfigService(
        store=WorkspaceConfigStore(tmp_path / "state"), knowledge=knowledge,
    )
    a = service.save_project("项目 A", first)
    b = service.save_project("项目 B", second)
    for project in (a, b):
        for domain in KnowledgeDomain:
            assert knowledge.load_branch(domain, project.id)[1] == ""
    knowledge.save_branch(KnowledgeDomain.DEVELOPMENT, a.id, "A 的业务规则")
    renamed = service.save_project("项目 A 新名称", moved, project_id=a.id)
    assert renamed.id == a.id
    assert renamed.owns(a.id, str(first))
    assert renamed.owns("旧知识名", str(first))
    assert not renamed.owns(b.id, str(second))
    assert not b.owns(None, str(first))
    reloaded = service.list_projects()
    assert reloaded[0].name == "项目 A 新名称"
    assert knowledge.load_branch(KnowledgeDomain.DEVELOPMENT, a.id)[1].strip() == "A 的业务规则"
    knowledge.save_branch(KnowledgeDomain.DEVELOPMENT, a.id, "")
    assert knowledge.load_branch(KnowledgeDomain.DEVELOPMENT, a.id)[1] == ""
    with pytest.raises(WorkspaceConfigError, match="名称已存在"):
        service.save_project("项目 B", tmp_path)
    with pytest.raises(WorkspaceConfigError, match="重复绑定"):
        service.save_project("新项目", first)


def test_legacy_migration_preserves_originals_and_never_overwrites_edits(tmp_path: Path) -> None:
    workspace = tmp_path / "legacy"
    workspace.mkdir()
    store = WorkspaceConfigStore(tmp_path / "state")
    store.save(workspace)
    original_config = store.path.read_bytes()
    knowledge = MarkdownKnowledgeService(tmp_path / "ace")
    source = knowledge.create_branch(KnowledgeDomain.DEVELOPMENT, "legacy")
    knowledge.save_branch(KnowledgeDomain.DEVELOPMENT, "legacy", "旧知识")
    service = WorkspaceConfigService(store=store, knowledge=knowledge)
    project = service.list_projects()[0]
    assert project.migrated
    assert knowledge.load_branch(KnowledgeDomain.DEVELOPMENT, project.id)[1].strip() == "旧知识"
    knowledge.save_branch(KnowledgeDomain.DEVELOPMENT, project.id, "新知识")
    assert service.list_projects()[0].id == project.id
    assert knowledge.load_branch(KnowledgeDomain.DEVELOPMENT, project.id)[1].strip() == "新知识"
    assert source.read_text(encoding="utf-8").strip() == "旧知识"
    assert store.path.read_bytes() == original_config
    workspace.rmdir()
    assert not store.load().available


def test_ambiguous_legacy_knowledge_is_preserved_without_guessing(tmp_path: Path) -> None:
    workspace = tmp_path / "legacy"
    workspace.mkdir()
    store = WorkspaceConfigStore(tmp_path / "state")
    store.save(workspace)
    knowledge = MarkdownKnowledgeService(tmp_path / "ace")
    for name in ("one", "two"):
        knowledge.create_branch(KnowledgeDomain.DEVELOPMENT, name)
    project = WorkspaceConfigService(store=store, knowledge=knowledge).list_projects()[0]
    assert knowledge.load_branch(KnowledgeDomain.DEVELOPMENT, project.id)[1] == ""
    assert knowledge.branch_path(KnowledgeDomain.DEVELOPMENT, "one").exists()
    assert knowledge.branch_path(KnowledgeDomain.DEVELOPMENT, "two").exists()


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
