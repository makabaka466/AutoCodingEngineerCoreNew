"""Ports that keep RAG orchestration independent from Ollama and Qdrant."""

from __future__ import annotations

from typing import Protocol

from autocoding_agent.knowledge_rag.models import (
    KnowledgeDomain,
    KnowledgeRetrievalResult,
    VectorMatch,
    VectorPoint,
)


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    @property
    def simulated(self) -> bool: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...


class VectorStore(Protocol):
    def replace_document(self, document_id: str, points: list[VectorPoint]) -> None: ...

    def delete_document(self, document_id: str) -> None: ...

    def search(
        self,
        vector: list[float],
        *,
        domain: KnowledgeDomain,
        project: str | None,
        workspace_id: str | None,
        limit: int,
    ) -> list[VectorMatch]: ...


class KnowledgeRetriever(Protocol):
    def retrieve(
        self,
        query: str,
        *,
        domain: KnowledgeDomain,
        project: str | None = None,
        workspace_id: str | None = None,
        limit: int = 6,
    ) -> KnowledgeRetrievalResult: ...
