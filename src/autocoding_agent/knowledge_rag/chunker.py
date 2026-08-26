"""Markdown-aware chunking that preserves engineering section context."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

from autocoding_agent.knowledge_rag.models import KnowledgeChunk, KnowledgeDocument

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CJK = re.compile(r"[\u3400-\u9fff]")


@dataclass(frozen=True)
class _Section:
    heading_path: str
    content: str


class MarkdownChunker:
    def __init__(
        self,
        target_tokens: int = 750,
        max_tokens: int = 1200,
        min_tokens: int = 120,
        overlap_tokens: int = 80,
    ) -> None:
        if not 1 <= min_tokens <= target_tokens <= max_tokens:
            raise ValueError("Chunk token limits must satisfy min <= target <= max.")
        self.target_tokens = target_tokens
        self.max_tokens = max_tokens
        self.min_tokens = min_tokens
        self.overlap_tokens = overlap_tokens

    def split(self, document: KnowledgeDocument, markdown: str) -> list[KnowledgeChunk]:
        sections = self._sections(self._without_frontmatter(markdown))
        pieces: list[tuple[str, str]] = []
        for section in sections:
            packed = self._pack_blocks(self._blocks(section.content))
            for item in packed:
                if item.strip():
                    pieces.append((section.heading_path, item.strip()))
        pieces = self._merge_short_pieces(pieces)
        chunks: list[KnowledgeChunk] = []
        previous_heading = ""
        previous_content = ""
        for ordinal, (heading, content) in enumerate(pieces):
            if previous_content and previous_heading == heading and self.overlap_tokens:
                overlap = self._tail(previous_content, self.overlap_tokens)
                if overlap and overlap not in content:
                    content = f"{overlap}\n\n{content}"
            embedding_text = self._embedding_text(document, heading, content)
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            chunk_id = hashlib.sha256(
                f"{document.id}:{heading}:{ordinal}:{content_hash}".encode("utf-8")
            ).hexdigest()
            chunks.append(
                KnowledgeChunk(
                    id=chunk_id,
                    document_id=document.id,
                    ordinal=ordinal,
                    title=document.title,
                    heading_path=heading,
                    content=content,
                    embedding_text=embedding_text,
                    content_hash=content_hash,
                    approximate_tokens=max(1, approximate_tokens(content)),
                    domain=document.domain,
                    project=document.project,
                    workspace_id=document.workspace_id,
                    source_type=document.source_type,
                    source_path=document.display_path,
                )
            )
            previous_heading = heading
            previous_content = content
        return chunks

    @staticmethod
    def _without_frontmatter(markdown: str) -> str:
        lines = markdown.splitlines()
        if not lines or lines[0].strip() != "---":
            return markdown
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return "\n".join(lines[index + 1 :])
        return markdown

    @staticmethod
    def _sections(markdown: str) -> list[_Section]:
        headings: list[str] = []
        current_lines: list[str] = []
        current_path = ""
        sections: list[_Section] = []

        def flush() -> None:
            content = "\n".join(current_lines).strip()
            if content:
                sections.append(_Section(current_path, content))

        for line in markdown.splitlines():
            match = _HEADING.match(line)
            if not match:
                current_lines.append(line)
                continue
            flush()
            current_lines = []
            level = len(match.group(1))
            title = match.group(2).strip()
            headings = headings[: level - 1]
            while len(headings) < level - 1:
                headings.append("")
            headings.append(title)
            current_path = " > ".join(item for item in headings if item)
        flush()
        return sections or [_Section("", markdown.strip())]

    @staticmethod
    def _blocks(content: str) -> list[str]:
        blocks: list[str] = []
        current: list[str] = []
        fence: str | None = None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                marker = stripped[:3]
                fence = None if fence == marker else marker
                current.append(line)
                continue
            if not stripped and fence is None:
                if current:
                    blocks.append("\n".join(current).strip())
                    current = []
                continue
            current.append(line)
        if current:
            blocks.append("\n".join(current).strip())
        return [item for item in blocks if item]

    def _pack_blocks(self, blocks: list[str]) -> list[str]:
        pieces: list[str] = []
        current: list[str] = []
        current_tokens = 0
        for block in blocks:
            block_tokens = approximate_tokens(block)
            if block_tokens > self.max_tokens:
                if current:
                    pieces.append("\n\n".join(current))
                    current = []
                    current_tokens = 0
                pieces.extend(self._split_oversized(block))
                continue
            if current and current_tokens + block_tokens > self.target_tokens:
                pieces.append("\n\n".join(current))
                current = []
                current_tokens = 0
            current.append(block)
            current_tokens += block_tokens
        if current:
            pieces.append("\n\n".join(current))
        return pieces

    def _split_oversized(self, text: str) -> list[str]:
        lines = text.splitlines() or [text]
        pieces: list[str] = []
        current: list[str] = []
        for line in lines:
            if current and approximate_tokens("\n".join([*current, line])) > self.max_tokens:
                pieces.append("\n".join(current))
                current = []
            if approximate_tokens(line) > self.max_tokens:
                char_limit = max(200, self.max_tokens * 3)
                pieces.extend(
                    line[index : index + char_limit]
                    for index in range(0, len(line), char_limit)
                )
            else:
                current.append(line)
        if current:
            pieces.append("\n".join(current))
        return pieces

    def _merge_short_pieces(
        self, pieces: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        merged: list[tuple[str, str]] = []
        for heading, content in pieces:
            if (
                merged
                and approximate_tokens(content) < self.min_tokens
                and merged[-1][0] == heading
                and approximate_tokens(merged[-1][1] + content) <= self.max_tokens
            ):
                old_heading, old_content = merged[-1]
                merged[-1] = (old_heading, f"{old_content}\n\n{content}")
            else:
                merged.append((heading, content))
        return merged

    @staticmethod
    def _tail(text: str, token_limit: int) -> str:
        lines = text.splitlines()
        selected: list[str] = []
        for line in reversed(lines):
            candidate = "\n".join([line, *selected])
            if selected and approximate_tokens(candidate) > token_limit:
                break
            selected.insert(0, line)
        return "\n".join(selected).strip()

    @staticmethod
    def _embedding_text(
        document: KnowledgeDocument,
        heading: str,
        content: str,
    ) -> str:
        metadata = [
            f"Document: {document.title}",
            f"Source Type: {document.source_type.value}",
            f"Domain: {document.domain.value}",
        ]
        if document.project:
            metadata.append(f"Project: {document.project}")
        if heading:
            metadata.append(f"Heading: {heading}")
        return "\n".join(metadata) + "\n\n" + content


def approximate_tokens(text: str) -> int:
    cjk = len(_CJK.findall(text))
    ascii_count = len(_CJK.sub("", text).encode("utf-8", errors="ignore"))
    return cjk + math.ceil(ascii_count / 4)
