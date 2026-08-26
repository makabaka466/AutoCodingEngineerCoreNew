from __future__ import annotations

from pathlib import Path

from autocoding_agent.config import Settings
from autocoding_agent.knowledge_rag.chunker import MarkdownChunker
from autocoding_agent.knowledge_rag.models import (
    KnowledgeDomain,
    KnowledgeIndexStatus,
)
from autocoding_agent.knowledge_rag.service import build_fake_rag_service


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "agent-project"
    knowledge = root / "knowledge" / "development" / "生物"
    knowledge.mkdir(parents=True)
    (root / "knowledge" / "incident").mkdir(parents=True)
    (root / "docs").mkdir()
    (knowledge / "生物.md").write_text(
        """# 生物项目知识

## SQL Server 查询超时

pyodbc 查询必须设置 60 秒超时，未知数据量时先查询 100 条。

## 页面定位

使用 Menu.URL 定位 CustomYieldUpLoad.cs，不要扫描整个仓库。
""",
        encoding="utf-8",
    )
    (root / "docs" / "PROJECT_EXPERIENCE.md").write_text(
        "# 工程经验\n\n## 安全修改\n\n修改前先给出方案并获得批准。\n",
        encoding="utf-8",
    )
    return root


def test_manual_rag_discovery_does_not_index_documents_automatically(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    service = build_fake_rag_service(
        Settings(data_dir=tmp_path / "data"),
        project_root=project,
    )

    documents = service.refresh_documents()

    assert len(documents) == 2
    assert {item.status for item in documents} == {KnowledgeIndexStatus.PENDING}
    assert service.repository.get_chunks(documents[0].id) == []
    assert service.simulated is True
    assert service.model_id == "fake-hash-embedding-v1"


def test_markdown_chunking_preserves_heading_context_and_limits_size(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    service = build_fake_rag_service(
        Settings(data_dir=tmp_path / "data"),
        project_root=project,
    )
    document = next(
        item for item in service.refresh_documents() if item.project == "生物"
    )
    service.chunker = MarkdownChunker(
        target_tokens=20,
        max_tokens=40,
        min_tokens=1,
        overlap_tokens=4,
    )

    chunks = service.preview_chunks(document.id)

    assert chunks
    assert any("SQL Server 查询超时" in item.heading_path for item in chunks)
    assert any("Menu.URL" in item.content for item in chunks)
    assert all(item.approximate_tokens <= 50 for item in chunks)
    assert all("Document: 生物项目知识" in item.embedding_text for item in chunks)


def test_manual_index_builds_vector_and_fts_then_marks_document_outdated(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    service = build_fake_rag_service(
        Settings(data_dir=tmp_path / "data"),
        project_root=project,
    )
    document = next(
        item for item in service.refresh_documents() if item.project == "生物"
    )

    receipt = service.index_document(document.id)
    result = service.retrieve(
        "CustomYieldUpLoad.cs Menu.URL 页面在哪里",
        domain=KnowledgeDomain.DEVELOPMENT,
        project="生物",
    )
    unscoped_result = service.retrieve(
        "CustomYieldUpLoad.cs Menu.URL 页面在哪里",
        domain=KnowledgeDomain.DEVELOPMENT,
    )
    indexed = service.repository.get_document(document.id)

    assert receipt.chunk_count >= 2
    assert receipt.simulated is True
    assert indexed is not None
    assert indexed.status == KnowledgeIndexStatus.INDEXED
    assert result.hits
    assert all(hit.project is None for hit in unscoped_result.hits)
    assert any("Menu.URL" in hit.content for hit in result.hits)
    assert result.prompt_context().startswith("The host retrieved")
    assert "simulated embedding provider" in result.prompt_context()

    source = Path(document.source_path)
    source.write_text(
        source.read_text(encoding="utf-8") + "\n## 新经验\n\n新增内容。\n",
        encoding="utf-8",
    )
    refreshed = {item.id: item for item in service.refresh_documents()}
    stale_result = service.retrieve(
        "CustomYieldUpLoad.cs Menu.URL",
        domain=KnowledgeDomain.DEVELOPMENT,
        project="生物",
    )

    assert refreshed[document.id].status == KnowledgeIndexStatus.OUTDATED
    assert stale_result.hits == []


def test_remove_index_keeps_source_markdown_and_excludes_it_from_retrieval(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    service = build_fake_rag_service(
        Settings(data_dir=tmp_path / "data"),
        project_root=project,
    )
    document = next(
        item for item in service.refresh_documents() if item.project == "生物"
    )
    service.index_document(document.id)

    service.remove_document(document.id)
    state = service.repository.get_document(document.id)
    result = service.retrieve(
        "Menu.URL",
        domain=KnowledgeDomain.DEVELOPMENT,
        project="生物",
    )

    assert Path(document.source_path).is_file()
    assert state is not None
    assert state.status == KnowledgeIndexStatus.REMOVED
    assert state.chunk_count == 0
    assert result.hits == []


def test_capability_documents_are_discovered_as_pending(tmp_path: Path) -> None:
    project = _project(tmp_path)
    data_dir = tmp_path / "data"
    capability = (
        data_dir
        / "workspaces"
        / "workspace-1"
        / "incident"
        / "capabilities"
        / "session-1.md"
    )
    capability.parent.mkdir(parents=True)
    capability.write_text("# 订单异常诊断\n\n发现事务没有提交。\n", encoding="utf-8")
    task = capability.parent.parent / "tasks" / "session-1.json"
    task.parent.mkdir()
    task.write_text('{"project":"生物"}', encoding="utf-8")
    service = build_fake_rag_service(
        Settings(data_dir=data_dir),
        project_root=project,
    )

    documents = service.refresh_documents()
    found = next(item for item in documents if item.title == "订单异常诊断")

    assert found.domain == KnowledgeDomain.INCIDENT
    assert found.project == "生物"
    assert found.workspace_id == "workspace-1"
    assert found.status == KnowledgeIndexStatus.PENDING
