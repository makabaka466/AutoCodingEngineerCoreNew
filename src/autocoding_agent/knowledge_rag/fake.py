"""Deterministic local stand-ins used until Ollama and Qdrant are deployed."""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from array import array
from pathlib import Path

from autocoding_agent.knowledge_rag.models import (
    KnowledgeDomain,
    VectorMatch,
    VectorPoint,
)

_TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]*|[\u3400-\u9fff]")


class FakeEmbeddingProvider:
    """Hash tokens into normalized vectors; useful for contracts, never quality claims."""

    model_id = "fake-hash-embedding-v1"
    dimension = 96
    simulated = True

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, query: str) -> list[float]:
        return self._embed(query)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = [item.casefold() for item in _TOKEN_PATTERN.findall(text)]
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            slot = int.from_bytes(digest[:4], "little") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[slot] += sign
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude == 0:
            return vector
        return [value / magnitude for value in vector]


class SQLiteFakeVectorStore:
    """Persist simulated vectors separately so future Qdrant indexes cannot be confused."""

    def __init__(self, database_path: str | Path, model_id: str) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.model_id = model_id
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def replace_document(self, document_id: str, points: list[VectorPoint]) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM fake_vectors WHERE document_id = ? AND model_id = ?",
                (document_id, self.model_id),
            )
            connection.executemany(
                """
                INSERT INTO fake_vectors(
                    chunk_id, document_id, model_id, dimension, vector,
                    domain, project, workspace_id, source_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        point.id,
                        point.document_id,
                        self.model_id,
                        len(point.vector),
                        array("f", point.vector).tobytes(),
                        point.domain.value,
                        point.project,
                        point.workspace_id,
                        point.source_type.value,
                    )
                    for point in points
                ],
            )

    def delete_document(self, document_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM fake_vectors WHERE document_id = ? AND model_id = ?",
                (document_id, self.model_id),
            )

    def search(
        self,
        vector: list[float],
        *,
        domain: KnowledgeDomain,
        project: str | None,
        workspace_id: str | None,
        limit: int,
    ) -> list[VectorMatch]:
        clauses = ["model_id = ?", "domain IN (?, ?)"]
        parameters: list[object] = [
            self.model_id,
            domain.value,
            KnowledgeDomain.GENERAL.value,
        ]
        if project:
            clauses.append("(project IS NULL OR project = ?)")
            parameters.append(project)
        else:
            clauses.append("project IS NULL")
        if workspace_id:
            clauses.append("(workspace_id IS NULL OR workspace_id = ?)")
            parameters.append(workspace_id)
        query = (
            "SELECT chunk_id, vector, dimension FROM fake_vectors WHERE "
            + " AND ".join(clauses)
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        scored: list[tuple[str, float]] = []
        for row in rows:
            stored = array("f")
            stored.frombytes(row["vector"])
            if len(stored) != len(vector) or len(stored) != row["dimension"]:
                continue
            score = sum(left * right for left, right in zip(vector, stored, strict=True))
            scored.append((str(row["chunk_id"]), score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return [
            VectorMatch(chunk_id=chunk_id, rank=rank, score=score)
            for rank, (chunk_id, score) in enumerate(scored[:limit], start=1)
        ]

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fake_vectors(
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    domain TEXT NOT NULL,
                    project TEXT,
                    workspace_id TEXT,
                    source_type TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_fake_vectors_document "
                "ON fake_vectors(document_id, model_id)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection
