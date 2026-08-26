from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from autocoding_agent.embedding_setup import EmbeddingConnectionConfig
from autocoding_agent.knowledge_rag import voyage
from autocoding_agent.knowledge_rag.voyage import (
    VoyageEmbeddingError,
    VoyageEmbeddingProvider,
)


def _response(count: int, dimension: int) -> dict[str, object]:
    return {
        "data": [
            {
                "index": index,
                "embedding": [float(index + 1)] * dimension,
            }
            for index in range(count)
        ]
    }


def test_voyage_provider_uses_bearer_auth_and_input_types() -> None:
    calls: list[tuple[dict[str, object], str | None, int]] = []

    def transport(request: Request, timeout: int) -> dict[str, object]:
        payload = json.loads((request.data or b"").decode("utf-8"))
        calls.append((payload, request.get_header("Authorization"), timeout))
        return _response(len(payload["input"]), 4)

    config = EmbeddingConnectionConfig(
        endpoint="https://api.voyageai.com/v1/embeddings",
        model="voyage-code-4",
        output_dimension=4,
        request_timeout_seconds=27,
    )
    provider = VoyageEmbeddingProvider(
        config=config,
        api_key="voyage-secret",
        transport=transport,
    )

    documents = provider.embed_documents(["document one", "document two"])
    query = provider.embed_query("find document one")

    assert documents == [[1.0] * 4, [2.0] * 4]
    assert query == [1.0] * 4
    assert calls[0][0]["input_type"] == "document"
    assert calls[1][0]["input_type"] == "query"
    assert all(call[0]["output_dimension"] == 4 for call in calls)
    assert all(call[1] == "Bearer voyage-secret" for call in calls)
    assert all(call[2] == 27 for call in calls)


def test_voyage_provider_batches_documents_and_preserves_order() -> None:
    batch_sizes: list[int] = []

    def transport(request: Request, _timeout: int) -> dict[str, object]:
        payload = json.loads((request.data or b"").decode("utf-8"))
        count = len(payload["input"])
        batch_sizes.append(count)
        return _response(count, 2)

    provider = VoyageEmbeddingProvider(
        config=EmbeddingConnectionConfig(output_dimension=2),
        api_key="secret",
        transport=transport,
        batch_size=2,
    )

    vectors = provider.embed_documents(["a", "b", "c"])

    assert batch_sizes == [2, 1]
    assert vectors == [[1.0, 1.0], [2.0, 2.0], [1.0, 1.0]]


def test_voyage_provider_rejects_wrong_dimensions_without_exposing_key() -> None:
    def transport(_request: Request, _timeout: int) -> dict[str, object]:
        return _response(1, 3)

    provider = VoyageEmbeddingProvider(
        config=EmbeddingConnectionConfig(output_dimension=4),
        api_key="must-not-leak",
        transport=transport,
    )

    with pytest.raises(VoyageEmbeddingError, match="dimension") as captured:
        provider.embed_query("query")

    assert "must-not-leak" not in str(captured.value)


def test_voyage_http_error_redacts_an_echoed_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "echoed-voyage-secret"

    def fail(_request: Request, timeout: int):
        assert timeout == 30
        raise HTTPError(
            "https://api.voyageai.com/v1/embeddings",
            401,
            "Unauthorized",
            {},
            BytesIO(json.dumps({"detail": f"invalid key {secret}"}).encode("utf-8")),
        )

    monkeypatch.setattr(voyage, "urlopen", fail)
    provider = VoyageEmbeddingProvider(
        config=EmbeddingConnectionConfig(output_dimension=4),
        api_key=secret,
    )

    with pytest.raises(VoyageEmbeddingError, match="HTTP 401") as captured:
        provider.embed_query("query")

    assert secret not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)
