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
    service.create_branch(workspace, KnowledgeDomain.DEVELOPMENT, "生物")
    service.create_branch(workspace, KnowledgeDomain.DEVELOPMENT, "组装")
    service.create_branch(workspace, KnowledgeDomain.INCIDENT, "生物")
    development_document = service.create_document(
        workspace,
        KnowledgeDomain.DEVELOPMENT,
        "生物",
        "handoff",
    )
    incident_document = service.create_document(
        workspace,
        KnowledgeDomain.INCIDENT,
        "生物",
        "diagnosis.md",
    )

    saved = service.save_document(
        workspace,
        KnowledgeDomain.DEVELOPMENT,
        "生物",
        "handoff.md",
        "# Development handoff\n\nCurrent facts.",
    )

    assert saved == development_document
    assert service.list_branches(workspace, KnowledgeDomain.DEVELOPMENT) == [
        "生物",
        "组装",
    ]
    assert service.list_branches(workspace, KnowledgeDomain.INCIDENT) == ["生物"]
    assert [
        item.name
        for item in service.list_documents(
            workspace, KnowledgeDomain.DEVELOPMENT, "生物"
        )
    ] == ["handoff.md"]
    development_index = (
        development_document.parents[2] / "CAPABILITIES.md"
    ).read_text(encoding="utf-8")
    incident_index = (incident_document.parents[2] / "CAPABILITIES.md").read_text(
        encoding="utf-8"
    )
    assert "pinned/生物/handoff.md" in development_index
    assert "diagnosis.md" not in development_index
    assert "pinned/生物/diagnosis.md" in incident_index
    assert "handoff.md" not in incident_index


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


def test_legacy_pinned_documents_can_move_into_a_secondary_branch(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    service = MarkdownKnowledgeService(tmp_path / "state")
    pinned = service.refresh_index(workspace, KnowledgeDomain.DEVELOPMENT) / "pinned"
    legacy = pinned / "legacy.md"
    legacy.write_text("# Legacy guide\n", encoding="utf-8")

    moved = service.migrate_root_documents(
        workspace,
        KnowledgeDomain.DEVELOPMENT,
        "生物",
    )

    assert moved == [pinned / "生物" / "legacy.md"]
    assert not legacy.exists()
    assert "pinned/生物/legacy.md" in (
        pinned.parent / "CAPABILITIES.md"
    ).read_text(encoding="utf-8")
