"""SQLite source metadata, chunks, and FTS5 lexical retrieval."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from autocoding_agent.knowledge_rag.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexStatus,
)

_SEARCH_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]*|[\u3400-\u9fff]")


class SQLiteKnowledgeRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def sync_discovered(self, document: KnowledgeDocument) -> KnowledgeDocument:
        existing = self.get_document(document.id)
        if existing is None:
            status = KnowledgeIndexStatus.PENDING
            indexed_hash = None
            chunk_count = 0
            embedding_model = None
            indexed_at = None
            last_error = None
        else:
            indexed_hash = existing.indexed_hash
            chunk_count = existing.chunk_count
            embedding_model = existing.embedding_model
            indexed_at = existing.indexed_at
            last_error = existing.last_error
            if existing.status == KnowledgeIndexStatus.REMOVED and (
                existing.current_hash == document.current_hash
            ):
                status = KnowledgeIndexStatus.REMOVED
            elif indexed_hash == document.current_hash:
                status = KnowledgeIndexStatus.INDEXED
                last_error = None
            elif (
                existing.status == KnowledgeIndexStatus.FAILED
                and existing.current_hash == document.current_hash
            ):
                status = KnowledgeIndexStatus.FAILED
            else:
                status = (
                    KnowledgeIndexStatus.OUTDATED
                    if indexed_hash
                    else KnowledgeIndexStatus.PENDING
                )
                last_error = None
        synced = document.model_copy(
            update={
                "indexed_hash": indexed_hash,
                "status": status,
                "chunk_count": chunk_count,
                "embedding_model": embedding_model,
                "indexed_at": indexed_at,
                "last_error": last_error,
            }
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_documents(
                    id, source_path, display_path, title, source_type, domain, project,
                    workspace_id, current_hash, indexed_hash, status, chunk_count,
                    embedding_model, last_error, source_updated_at, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_path=excluded.source_path,
                    display_path=excluded.display_path,
                    title=excluded.title,
                    source_type=excluded.source_type,
                    domain=excluded.domain,
                    project=excluded.project,
                    workspace_id=excluded.workspace_id,
                    current_hash=excluded.current_hash,
                    indexed_hash=excluded.indexed_hash,
                    status=excluded.status,
                    chunk_count=excluded.chunk_count,
                    embedding_model=excluded.embedding_model,
                    last_error=excluded.last_error,
                    source_updated_at=excluded.source_updated_at,
                    indexed_at=excluded.indexed_at
                """,
                self._document_values(synced),
            )
        return synced

    def list_documents(self) -> list[KnowledgeDocument]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_documents ORDER BY source_type, domain, title"
            ).fetchall()
        return [self._document_from_row(row) for row in rows]

    def get_document(self, document_id: str) -> KnowledgeDocument | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_documents WHERE id = ?", (document_id,)
            ).fetchone()
        return self._document_from_row(row) if row else None

    def mark_indexing(self, document_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE knowledge_documents SET status = ?, last_error = NULL WHERE id = ?",
                (KnowledgeIndexStatus.INDEXING.value, document_id),
            )

    def complete_index(
        self,
        document: KnowledgeDocument,
        chunks: list[KnowledgeChunk],
        embedding_model: str,
        indexed_at: str,
    ) -> None:
        with self._connect() as connection:
            old_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM knowledge_chunks WHERE document_id = ?", (document.id,)
                ).fetchall()
            ]
            connection.execute(
                "DELETE FROM knowledge_chunks WHERE document_id = ?", (document.id,)
            )
            for chunk_id in old_ids:
                connection.execute(
                    "DELETE FROM knowledge_chunks_fts WHERE chunk_id = ?", (chunk_id,)
                )
            connection.executemany(
                """
                INSERT INTO knowledge_chunks(
                    id, document_id, ordinal, title, heading_path, content,
                    embedding_text, content_hash, approximate_tokens, domain,
                    project, workspace_id, source_type, source_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.id,
                        chunk.document_id,
                        chunk.ordinal,
                        chunk.title,
                        chunk.heading_path,
                        chunk.content,
                        chunk.embedding_text,
                        chunk.content_hash,
                        chunk.approximate_tokens,
                        chunk.domain.value,
                        chunk.project,
                        chunk.workspace_id,
                        chunk.source_type.value,
                        chunk.source_path,
                    )
                    for chunk in chunks
                ],
            )
            connection.executemany(
                "INSERT INTO knowledge_chunks_fts(chunk_id, search_text) VALUES (?, ?)",
                [
                    (
                        chunk.id,
                        _search_terms(
                            " ".join(
                                [
                                    chunk.title,
                                    chunk.heading_path,
                                    chunk.source_path,
                                    chunk.content,
                                ]
                            )
                        ),
                    )
                    for chunk in chunks
                ],
            )
            connection.execute(
                """
                UPDATE knowledge_documents
                SET indexed_hash = current_hash, status = ?, chunk_count = ?,
                    embedding_model = ?, last_error = NULL, indexed_at = ?
                WHERE id = ?
                """,
                (
                    KnowledgeIndexStatus.INDEXED.value,
                    len(chunks),
                    embedding_model,
                    indexed_at,
                    document.id,
                ),
            )

    def mark_failed(self, document_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE knowledge_documents SET status = ?, last_error = ? WHERE id = ?",
                (KnowledgeIndexStatus.FAILED.value, " ".join(error.split())[:800], document_id),
            )

    def remove_index(self, document_id: str) -> None:
        with self._connect() as connection:
            chunk_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM knowledge_chunks WHERE document_id = ?", (document_id,)
                ).fetchall()
            ]
            connection.execute(
                "DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,)
            )
            for chunk_id in chunk_ids:
                connection.execute(
                    "DELETE FROM knowledge_chunks_fts WHERE chunk_id = ?", (chunk_id,)
                )
            connection.execute(
                """
                UPDATE knowledge_documents
                SET indexed_hash = NULL, status = ?, chunk_count = 0,
                    embedding_model = NULL, last_error = NULL, indexed_at = NULL
                WHERE id = ?
                """,
                (KnowledgeIndexStatus.REMOVED.value, document_id),
            )

    def get_chunks(self, document_id: str) -> list[KnowledgeChunk]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_chunks WHERE document_id = ? ORDER BY ordinal",
                (document_id,),
            ).fetchall()
        return [self._chunk_from_row(row) for row in rows]

    def get_chunks_by_ids(self, chunk_ids: list[str]) -> dict[str, KnowledgeChunk]:
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM knowledge_chunks WHERE id IN ({placeholders})",  # noqa: S608
                chunk_ids,
            ).fetchall()
        return {str(row["id"]): self._chunk_from_row(row) for row in rows}

    def indexed_document_ids(self, document_ids: list[str]) -> set[str]:
        if not document_ids:
            return set()
        placeholders = ",".join("?" for _ in document_ids)
        parameters = [*document_ids, KnowledgeIndexStatus.INDEXED.value]
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT id FROM knowledge_documents WHERE id IN ({placeholders}) "
                "AND status = ?",  # noqa: S608
                parameters,
            ).fetchall()
        return {str(row["id"]) for row in rows}

    def keyword_search(self, query: str, limit: int = 20) -> list[str]:
        terms = list(dict.fromkeys(_SEARCH_TOKEN.findall(query.casefold())))
        if not terms:
            return []
        match = " OR ".join(f'"{item.replace(chr(34), chr(34) * 2)}"' for item in terms[:32])
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chunk_id
                FROM knowledge_chunks_fts
                WHERE knowledge_chunks_fts MATCH ?
                ORDER BY bm25(knowledge_chunks_fts)
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        return [str(row["chunk_id"]) for row in rows]

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_documents(
                    id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL UNIQUE,
                    display_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    project TEXT,
                    workspace_id TEXT,
                    current_hash TEXT NOT NULL,
                    indexed_hash TEXT,
                    status TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    embedding_model TEXT,
                    last_error TEXT,
                    source_updated_at TEXT NOT NULL,
                    indexed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS knowledge_chunks(
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    heading_path TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    approximate_tokens INTEGER NOT NULL,
                    domain TEXT NOT NULL,
                    project TEXT,
                    workspace_id TEXT,
                    source_type TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document
                ON knowledge_chunks(document_id, ordinal);

                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    search_text,
                    tokenize='unicode61'
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @staticmethod
    def _document_values(document: KnowledgeDocument) -> tuple[object, ...]:
        return (
            document.id,
            document.source_path,
            document.display_path,
            document.title,
            document.source_type.value,
            document.domain.value,
            document.project,
            document.workspace_id,
            document.current_hash,
            document.indexed_hash,
            document.status.value,
            document.chunk_count,
            document.embedding_model,
            document.last_error,
            document.source_updated_at.isoformat(),
            document.indexed_at.isoformat() if document.indexed_at else None,
        )

    @staticmethod
    def _document_from_row(row: sqlite3.Row) -> KnowledgeDocument:
        return KnowledgeDocument(
            id=row["id"],
            source_path=row["source_path"],
            display_path=row["display_path"],
            title=row["title"],
            source_type=row["source_type"],
            domain=row["domain"],
            project=row["project"],
            workspace_id=row["workspace_id"],
            current_hash=row["current_hash"],
            indexed_hash=row["indexed_hash"],
            status=row["status"],
            chunk_count=row["chunk_count"],
            embedding_model=row["embedding_model"],
            last_error=row["last_error"],
            source_updated_at=row["source_updated_at"],
            indexed_at=row["indexed_at"],
        )

    @staticmethod
    def _chunk_from_row(row: sqlite3.Row) -> KnowledgeChunk:
        return KnowledgeChunk(
            id=row["id"],
            document_id=row["document_id"],
            ordinal=row["ordinal"],
            title=row["title"],
            heading_path=row["heading_path"],
            content=row["content"],
            embedding_text=row["embedding_text"],
            content_hash=row["content_hash"],
            approximate_tokens=row["approximate_tokens"],
            domain=row["domain"],
            project=row["project"],
            workspace_id=row["workspace_id"],
            source_type=row["source_type"],
            source_path=row["source_path"],
        )


def _search_terms(text: str) -> str:
    return " ".join(item.casefold() for item in _SEARCH_TOKEN.findall(text))
