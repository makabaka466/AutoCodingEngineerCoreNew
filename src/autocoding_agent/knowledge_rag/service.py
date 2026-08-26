"""Manual ingestion, hybrid retrieval, and source discovery orchestration."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from autocoding_agent.config import Settings, get_settings
from autocoding_agent.knowledge_rag.chunker import MarkdownChunker
from autocoding_agent.knowledge_rag.fake import (
    FakeEmbeddingProvider,
    SQLiteFakeVectorStore,
)
from autocoding_agent.knowledge_rag.models import (
    KnowledgeDocument,
    KnowledgeDomain,
    KnowledgeHit,
    KnowledgeIndexReceipt,
    KnowledgeRetrievalResult,
    KnowledgeSourceType,
    VectorPoint,
)
from autocoding_agent.knowledge_rag.ports import EmbeddingProvider, VectorStore
from autocoding_agent.knowledge_rag.repository import SQLiteKnowledgeRepository
from autocoding_agent.knowledge_rag.vector_store import SQLiteVectorStore
from autocoding_agent.workspace_knowledge import PROJECT_ROOT


class KnowledgeRAGService:
    def __init__(
        self,
        repository: SQLiteKnowledgeRepository,
        embeddings: EmbeddingProvider,
        vectors: VectorStore,
        *,
        data_dir: str | Path,
        project_root: str | Path = PROJECT_ROOT,
        chunker: MarkdownChunker | None = None,
    ) -> None:
        self.repository = repository
        self.embeddings = embeddings
        self.vectors = vectors
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.project_root = Path(project_root).expanduser().resolve()
        self.chunker = chunker or MarkdownChunker()

    @property
    def simulated(self) -> bool:
        return self.embeddings.simulated

    @property
    def model_id(self) -> str:
        return self.embeddings.model_id

    def refresh_documents(self) -> list[KnowledgeDocument]:
        discovered = [*self._project_documents(), *self._capability_documents()]
        for document in discovered:
            self.repository.sync_discovered(document)
        discovered_ids = {item.id for item in discovered}
        return [
            document
            for document in self.repository.list_documents()
            if document.id in discovered_ids
        ]

    def preview_chunks(self, document_id: str) -> list:
        document = self._require_document(document_id)
        markdown = Path(document.source_path).read_text(encoding="utf-8")
        return self.chunker.split(document, markdown)

    def index_document(self, document_id: str) -> KnowledgeIndexReceipt:
        document = self._require_document(document_id)
        self.repository.mark_indexing(document.id)
        try:
            chunks = self.preview_chunks(document.id)
            if not chunks:
                raise ValueError("Markdown did not contain any indexable content.")
            vectors = self.embeddings.embed_documents(
                [chunk.embedding_text for chunk in chunks]
            )
            if len(vectors) != len(chunks):
                raise ValueError("Embedding provider returned an unexpected vector count.")
            if any(len(vector) != self.embeddings.dimension for vector in vectors):
                raise ValueError("Embedding provider returned an unexpected vector dimension.")
            points = [
                VectorPoint(
                    id=chunk.id,
                    document_id=document.id,
                    vector=vector,
                    domain=chunk.domain,
                    project=chunk.project,
                    workspace_id=chunk.workspace_id,
                    source_type=chunk.source_type,
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
            self.vectors.replace_document(document.id, points)
            indexed_at = datetime.now(timezone.utc)
            self.repository.complete_index(
                document,
                chunks,
                self.embeddings.model_id,
                indexed_at.isoformat(),
            )
            return KnowledgeIndexReceipt(
                document_id=document.id,
                chunk_count=len(chunks),
                embedding_model=self.embeddings.model_id,
                simulated=self.embeddings.simulated,
                indexed_at=indexed_at,
            )
        except Exception as exc:
            self.repository.mark_failed(document.id, str(exc))
            raise

    def remove_document(self, document_id: str) -> None:
        self._require_document(document_id)
        self.vectors.delete_document(document_id)
        self.repository.remove_index(document_id)

    def retrieve(
        self,
        query: str,
        *,
        domain: KnowledgeDomain,
        project: str | None = None,
        workspace_id: str | None = None,
        limit: int = 6,
    ) -> KnowledgeRetrievalResult:
        clean_query = query.strip()
        if not clean_query:
            return KnowledgeRetrievalResult(
                query=query,
                embedding_model=self.embeddings.model_id,
                simulated=self.embeddings.simulated,
            )
        if not self.repository.has_indexed_documents(
            domain=domain.value,
            project=project,
            workspace_id=workspace_id,
        ):
            return KnowledgeRetrievalResult(
                query=clean_query,
                embedding_model=self.embeddings.model_id,
                simulated=self.embeddings.simulated,
            )
        vector = self.embeddings.embed_query(clean_query)
        dense = self.vectors.search(
            vector,
            domain=domain,
            project=project,
            workspace_id=workspace_id,
            limit=20,
        )
        lexical_ids = self.repository.keyword_search(clean_query, limit=20)
        all_ids = list(
            dict.fromkeys([*(item.chunk_id for item in dense), *lexical_ids])
        )
        chunks = self.repository.get_chunks_by_ids(all_ids)
        indexed_documents = self.repository.indexed_document_ids(
            list({chunk.document_id for chunk in chunks.values()})
        )
        dense_ranks = {item.chunk_id: item.rank for item in dense}
        lexical_ranks = {
            chunk_id: rank for rank, chunk_id in enumerate(lexical_ids, start=1)
        }
        scored: list[tuple[str, float]] = []
        for chunk_id in all_ids:
            chunk = chunks.get(chunk_id)
            if (
                chunk is None
                or chunk.document_id not in indexed_documents
                or not _matches(chunk, domain, project, workspace_id)
            ):
                continue
            score = 0.0
            if dense_rank := dense_ranks.get(chunk_id):
                score += 1.0 / (60 + dense_rank)
            if lexical_rank := lexical_ranks.get(chunk_id):
                score += 1.0 / (60 + lexical_rank)
            scored.append((chunk_id, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        hits: list[KnowledgeHit] = []
        per_document: dict[str, int] = {}
        for chunk_id, score in scored:
            chunk = chunks[chunk_id]
            if per_document.get(chunk.document_id, 0) >= 2:
                continue
            per_document[chunk.document_id] = per_document.get(chunk.document_id, 0) + 1
            hits.append(
                KnowledgeHit(
                    chunk_id=chunk.id,
                    document_id=chunk.document_id,
                    title=chunk.title,
                    heading_path=chunk.heading_path,
                    content=chunk.content,
                    source_path=chunk.source_path,
                    source_type=chunk.source_type,
                    domain=chunk.domain,
                    project=chunk.project,
                    score=score,
                    dense_rank=dense_ranks.get(chunk.id),
                    lexical_rank=lexical_ranks.get(chunk.id),
                )
            )
            if len(hits) >= limit:
                break
        return KnowledgeRetrievalResult(
            query=clean_query,
            hits=hits,
            embedding_model=self.embeddings.model_id,
            simulated=self.embeddings.simulated,
        )

    def _require_document(self, document_id: str) -> KnowledgeDocument:
        self.refresh_documents()
        document = self.repository.get_document(document_id)
        if document is None:
            raise ValueError(f"Knowledge document was not found: {document_id}")
        path = Path(document.source_path)
        if not path.is_file():
            raise ValueError(f"Knowledge source is unavailable: {document.display_path}")
        return document

    def _project_documents(self) -> list[KnowledgeDocument]:
        documents: list[KnowledgeDocument] = []
        root = self.project_root / "knowledge"
        for domain in (KnowledgeDomain.DEVELOPMENT, KnowledgeDomain.INCIDENT):
            domain_root = root / domain.value
            for path in sorted(domain_root.glob("*/*.md")):
                project = path.parent.name
                documents.append(
                    self._document(
                        path,
                        display_path=path.relative_to(self.project_root).as_posix(),
                        source_type=KnowledgeSourceType.PROJECT,
                        domain=domain,
                        project=project,
                    )
                )
        experience = self.project_root / "docs" / "PROJECT_EXPERIENCE.md"
        if experience.is_file():
            documents.append(
                self._document(
                    experience,
                    display_path=experience.relative_to(self.project_root).as_posix(),
                    source_type=KnowledgeSourceType.ENGINEERING_EXPERIENCE,
                    domain=KnowledgeDomain.GENERAL,
                )
            )
        return documents

    def _capability_documents(self) -> list[KnowledgeDocument]:
        documents: list[KnowledgeDocument] = []
        workspaces = self.data_dir / "workspaces"
        for domain in (KnowledgeDomain.DEVELOPMENT, KnowledgeDomain.INCIDENT):
            pattern = f"*/{domain.value}/capabilities/*.md"
            for path in sorted(workspaces.glob(pattern)):
                domain_root = path.parent.parent
                task = domain_root / "tasks" / f"{path.stem}.json"
                metadata: dict[str, object] = {}
                if task.is_file():
                    try:
                        metadata = json.loads(task.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        metadata = {}
                workspace_id = domain_root.parent.name
                documents.append(
                    self._document(
                        path,
                        display_path=(
                            f"capability/{workspace_id}/{domain.value}/{path.name}"
                        ),
                        source_type=KnowledgeSourceType.CAPABILITY,
                        domain=domain,
                        project=(
                            str(metadata["project"])
                            if metadata.get("project")
                            else None
                        ),
                        workspace_id=workspace_id,
                    )
                )
        return documents

    @staticmethod
    def _document(
        path: Path,
        *,
        display_path: str,
        source_type: KnowledgeSourceType,
        domain: KnowledgeDomain,
        project: str | None = None,
        workspace_id: str | None = None,
    ) -> KnowledgeDocument:
        canonical = path.resolve()
        content = canonical.read_text(encoding="utf-8")
        title = _markdown_title(content) or canonical.stem
        identifier = hashlib.sha256(str(canonical).casefold().encode("utf-8")).hexdigest()
        modified = datetime.fromtimestamp(canonical.stat().st_mtime, tz=timezone.utc)
        return KnowledgeDocument(
            id=identifier,
            source_path=str(canonical),
            display_path=display_path,
            title=title,
            source_type=source_type,
            domain=domain,
            project=project,
            workspace_id=workspace_id,
            current_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source_updated_at=modified,
        )


def build_fake_rag_service(
    settings: Settings | None = None,
    *,
    project_root: str | Path = PROJECT_ROOT,
) -> KnowledgeRAGService:
    configured = settings or get_settings()
    database = configured.data_dir / "rag" / "knowledge-fake.db"
    embeddings = FakeEmbeddingProvider()
    return KnowledgeRAGService(
        SQLiteKnowledgeRepository(database),
        embeddings,
        SQLiteFakeVectorStore(database, embeddings.model_id),
        data_dir=configured.data_dir,
        project_root=project_root,
    )


def build_voyage_rag_service(
    embeddings: EmbeddingProvider,
    settings: Settings | None = None,
    *,
    index_id: str,
    project_root: str | Path = PROJECT_ROOT,
) -> KnowledgeRAGService:
    configured = settings or get_settings()
    database = configured.data_dir / "rag" / f"knowledge-voyage-{index_id}.db"
    return KnowledgeRAGService(
        SQLiteKnowledgeRepository(database),
        embeddings,
        SQLiteVectorStore(database, embeddings.model_id),
        data_dir=configured.data_dir,
        project_root=project_root,
    )


def build_configured_rag_service(
    settings: Settings | None = None,
    *,
    embedding_setup=None,
    project_root: str | Path = PROJECT_ROOT,
) -> KnowledgeRAGService:
    configured = settings or get_settings()
    if embedding_setup is None:
        from autocoding_agent.embedding_setup import EmbeddingSetupService

        embedding_setup = EmbeddingSetupService(configured)
    provider = embedding_setup.provider()
    if provider is None:
        return build_fake_rag_service(configured, project_root=project_root)
    return build_voyage_rag_service(
        provider,
        configured,
        index_id=provider.config.index_id,
        project_root=project_root,
    )


def _markdown_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def workspace_id_for(workspace: str | Path) -> str:
    canonical = str(Path(workspace).expanduser().resolve()).casefold()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _matches(chunk, domain, project, workspace_id) -> bool:
    if chunk.domain not in {domain, KnowledgeDomain.GENERAL}:
        return False
    if project and chunk.project not in {None, project}:
        return False
    if project is None and chunk.project is not None:
        return False
    return not workspace_id or chunk.workspace_id in {None, workspace_id}
