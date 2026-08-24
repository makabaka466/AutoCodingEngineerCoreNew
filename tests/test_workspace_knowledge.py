from __future__ import annotations

from pathlib import Path

import pytest

from autocoding_agent.workspace_knowledge import (
    KnowledgeDomain,
    MarkdownKnowledgeService,
    WorkspaceKnowledgeError,
)


def test_markdown_knowledge_is_separate_by_domain_and_branch(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    service = MarkdownKnowledgeService(tmp_path / "state")
    development_document = service.create_branch(
        workspace, KnowledgeDomain.DEVELOPMENT, "生物"
    )
    service.create_branch(workspace, KnowledgeDomain.DEVELOPMENT, "组装")
    incident_document = service.create_branch(
        workspace, KnowledgeDomain.INCIDENT, "生物"
    )

    saved = service.save_branch(
        workspace,
        KnowledgeDomain.DEVELOPMENT,
        "生物",
        "# Development handoff\n\nCurrent facts.",
    )

    assert saved == development_document
    assert service.list_branches(workspace, KnowledgeDomain.DEVELOPMENT) == [
        "生物",
        "组装",
    ]
    assert service.list_branches(workspace, KnowledgeDomain.INCIDENT) == ["生物"]
    development_index = (
        development_document.parent.parent / "CAPABILITIES.md"
    ).read_text(encoding="utf-8")
    incident_index = (incident_document.parent.parent / "CAPABILITIES.md").read_text(
        encoding="utf-8"
    )
    assert "pinned/生物.md" in development_index
    assert "pinned/生物.md" in incident_index
    assert development_document != incident_document


@pytest.mark.parametrize("unsafe", ["../escape", "bad/name", "CON", "name."])
def test_markdown_knowledge_rejects_unsafe_branch_names(
    tmp_path: Path,
    unsafe: str,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    service = MarkdownKnowledgeService(tmp_path / "state")

    with pytest.raises(WorkspaceKnowledgeError):
        service.create_branch(workspace, KnowledgeDomain.DEVELOPMENT, unsafe)


def test_legacy_branch_directory_collapses_to_one_branch_document(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    service = MarkdownKnowledgeService(tmp_path / "state")
    pinned = service.refresh_index(workspace, KnowledgeDomain.DEVELOPMENT) / "pinned"
    legacy_directory = pinned / "生物"
    legacy_directory.mkdir()
    legacy = legacy_directory / "project-guide.md"
    legacy.write_text("# Legacy guide\n", encoding="utf-8")

    moved = service.migrate_directory_branch(
        workspace,
        KnowledgeDomain.DEVELOPMENT,
        "生物",
    )

    assert moved == pinned / "生物.md"
    assert not legacy.exists()
    assert not legacy_directory.exists()
    assert "# Legacy guide" in moved.read_text(encoding="utf-8")
    assert "pinned/生物.md" in (
        pinned.parent / "CAPABILITIES.md"
    ).read_text(encoding="utf-8")
