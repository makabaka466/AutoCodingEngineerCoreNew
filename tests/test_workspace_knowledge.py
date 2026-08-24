from __future__ import annotations

from pathlib import Path

import pytest

from autocoding_agent.adapters.capability_store import CapabilityStore
from autocoding_agent.incident.capability_store import IncidentCapabilityStore
from autocoding_agent.workspace_knowledge import (
    KnowledgeDomain,
    MarkdownKnowledgeService,
    WorkspaceKnowledgeError,
)


def test_markdown_knowledge_is_project_local_and_separate_by_domain(
    tmp_path: Path,
) -> None:
    service = MarkdownKnowledgeService(tmp_path / "state")
    development_document = service.create_branch(KnowledgeDomain.DEVELOPMENT, "生物")
    service.create_branch(KnowledgeDomain.DEVELOPMENT, "组装")
    incident_document = service.create_branch(KnowledgeDomain.INCIDENT, "生物")

    saved = service.save_branch(
        KnowledgeDomain.DEVELOPMENT,
        "生物",
        "# Development handoff\n\nCurrent facts.",
    )

    assert saved == development_document
    assert service.list_branches(KnowledgeDomain.DEVELOPMENT) == ["生物", "组装"]
    assert service.list_branches(KnowledgeDomain.INCIDENT) == ["生物"]
    assert development_document == (
        tmp_path / "state" / "knowledge" / "development" / "生物" / "生物.md"
    )
    assert incident_document == (
        tmp_path / "state" / "knowledge" / "incident" / "生物" / "生物.md"
    )


def test_project_knowledge_is_synced_into_each_workspace_memory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    service = MarkdownKnowledgeService(state)
    service.create_branch(KnowledgeDomain.DEVELOPMENT, "生物")
    service.create_branch(KnowledgeDomain.DEVELOPMENT, "组装")
    service.create_branch(KnowledgeDomain.INCIDENT, "生物")
    workspace = tmp_path / "project"
    workspace.mkdir()

    development = CapabilityStore(
        state, knowledge_root=service.knowledge_root / "development"
    ).prepare(workspace, "生物")
    incident = IncidentCapabilityStore(
        state, knowledge_root=service.knowledge_root / "incident"
    ).prepare(workspace, "生物")

    assert (development / "pinned" / "生物" / "生物.md").is_file()
    assert not (development / "pinned" / "组装" / "组装.md").exists()
    assert (incident / "pinned" / "生物" / "生物.md").is_file()
    assert "pinned/生物/生物.md" in (development / "CAPABILITIES.md").read_text(
        encoding="utf-8"
    )
    assert "pinned/生物/生物.md" in (incident / "CAPABILITIES.md").read_text(
        encoding="utf-8"
    )

    CapabilityStore(
        state, knowledge_root=service.knowledge_root / "development"
    ).prepare(workspace, "组装")
    switched_index = (development / "CAPABILITIES.md").read_text(encoding="utf-8")
    assert "pinned/组装/组装.md" in switched_index
    assert "pinned/生物/生物.md" not in switched_index


@pytest.mark.parametrize("unsafe", ["../escape", "bad/name", "CON", "name."])
def test_markdown_knowledge_rejects_unsafe_branch_names(
    tmp_path: Path,
    unsafe: str,
) -> None:
    service = MarkdownKnowledgeService(tmp_path / "state")

    with pytest.raises(WorkspaceKnowledgeError):
        service.create_branch(KnowledgeDomain.DEVELOPMENT, unsafe)
