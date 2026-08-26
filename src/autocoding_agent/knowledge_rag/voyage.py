"""Minimal Voyage REST embedding adapter with no SDK dependency."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from autocoding_agent.embedding_setup import EmbeddingConnectionConfig


class VoyageEmbeddingError(RuntimeError):
    """A redacted provider or response-contract error."""


Transport = Callable[[Request, int], dict[str, object]]


class VoyageEmbeddingProvider:
    """Call Voyage's text embeddings endpoint for documents and retrieval queries."""

    simulated = False

    def __init__(
        self,
        *,
        config: EmbeddingConnectionConfig,
        api_key: str,
        transport: Transport | None = None,
        batch_size: int = 128,
    ) -> None:
        if not api_key.strip():
            raise VoyageEmbeddingError("Voyage API Key is missing.")
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        self.config = config
        self._api_key = api_key.strip()
        self._transport = transport or _send_json
        self.batch_size = batch_size

    @property
    def model_id(self) -> str:
        return self.config.model_id

    @property
    def dimension(self) -> int:
        return self.config.output_dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed(texts[start : start + self.batch_size], "document"))
        return vectors

    def embed_query(self, query: str) -> list[float]:
        vectors = self._embed([query], "query")
        if len(vectors) != 1:
            raise VoyageEmbeddingError("Voyage returned an unexpected query vector count.")
        return vectors[0]

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        if not texts:
            return []
        payload = json.dumps(
            {
                "input": texts,
                "model": self.config.model,
                "input_type": input_type,
                "truncation": True,
                "output_dimension": self.config.output_dimension,
                "output_dtype": "float",
            }
        ).encode("utf-8")
        request = Request(
            self.config.endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "AutoCodingEngineerCoreNew/0.6.1",
            },
            method="POST",
        )
        response = self._transport(request, self.config.request_timeout_seconds)
        return self._parse_vectors(response, len(texts))

    def _parse_vectors(
        self,
        response: dict[str, object],
        expected_count: int,
    ) -> list[list[float]]:
        raw_data = response.get("data")
        if not isinstance(raw_data, list):
            raise VoyageEmbeddingError("Voyage response is missing the data array.")
        indexed: list[tuple[int, list[float]]] = []
        for fallback_index, item in enumerate(raw_data):
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise VoyageEmbeddingError("Voyage response contains an invalid embedding item.")
            raw_vector = item["embedding"]
            try:
                vector = [float(value) for value in raw_vector]
            except (TypeError, ValueError) as exc:
                raise VoyageEmbeddingError("Voyage returned a non-numeric embedding.") from exc
            if len(vector) != self.dimension or not all(math.isfinite(value) for value in vector):
                raise VoyageEmbeddingError(
                    "Voyage returned an invalid or unexpected embedding dimension."
                )
            raw_index = item.get("index", fallback_index)
            if not isinstance(raw_index, int):
                raise VoyageEmbeddingError("Voyage returned an invalid embedding index.")
            indexed.append((raw_index, vector))
        indexed.sort(key=lambda item: item[0])
        if len(indexed) != expected_count or [item[0] for item in indexed] != list(
            range(expected_count)
        ):
            raise VoyageEmbeddingError("Voyage returned an unexpected embedding count or order.")
        return [vector for _, vector in indexed]


def _send_json(request: Request, timeout: int) -> dict[str, object]:
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read(8 * 1024 * 1024)
    except HTTPError as exc:
        detail = _safe_error_detail(exc.read(4096))
        authorization = request.get_header("Authorization") or ""
        if authorization.startswith("Bearer "):
            detail = detail.replace(authorization[7:], "[REDACTED]")
        suffix = f" · {detail}" if detail else ""
        raise VoyageEmbeddingError(f"Voyage API 返回 HTTP {exc.code}{suffix}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise VoyageEmbeddingError(f"无法连接 Voyage Embedding API：{exc}") from exc
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoyageEmbeddingError("Voyage API 返回了无效 JSON。") from exc
    if not isinstance(parsed, dict):
        raise VoyageEmbeddingError("Voyage API 返回格式无效。")
    return parsed


def _safe_error_detail(body: bytes) -> str:
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    detail = parsed.get("detail") or parsed.get("message")
    if detail is None and isinstance(parsed.get("error"), dict):
        detail = parsed["error"].get("message")
    if not isinstance(detail, str):
        return ""
    return " ".join(detail.split())[:300]
