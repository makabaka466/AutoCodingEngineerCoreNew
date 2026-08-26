"""Local persistent vector store for configured non-simulated providers."""

from __future__ import annotations

import sqlite3
from array import array
from pathlib import Path

from autocoding_agent.knowledge_rag.models import (
    KnowledgeDomain,
    VectorMatch,
    VectorPoint,
)


class SQLiteVectorStore:
    """A rebuildable local vector index isolated by embedding model identity."""

    def __init__(self, database_path: str | Path, model_id: str) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.model_id = model_id
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def replace_document(self, document_id: str, points: list[VectorPoint]) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM vectors WHERE document_id = ? AND model_id = ?",
                (document_id, self.model_id),
            )
            connection.executemany(
                """
                INSERT INTO vectors(
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
                "DELETE FROM vectors WHERE document_id = ? AND model_id = ?",
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
        query = "SELECT chunk_id, vector, dimension FROM vectors WHERE " + " AND ".join(
            clauses
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS vectors(
                    chunk_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    domain TEXT NOT NULL,
                    project TEXT,
                    workspace_id TEXT,
                    source_type TEXT NOT NULL,
                    PRIMARY KEY(chunk_id, model_id)
                );

                CREATE INDEX IF NOT EXISTS idx_vectors_document
                ON vectors(document_id, model_id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection
