from __future__ import annotations

from pathlib import Path

import pytest

from autocoding_agent.config import Settings
from autocoding_agent.embedding_setup import (
    DEFAULT_EMBEDDING_ENDPOINT,
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingConfigStore,
    EmbeddingConnectionConfig,
    EmbeddingSetupError,
    EmbeddingSetupService,
)


class FakeSecretStore:
    def __init__(self, secret: str | None = None) -> None:
        self.secret = secret
        self.saved: list[str] = []

    def get(self) -> str | None:
        return self.secret

    def set(self, secret: str) -> None:
        self.secret = secret
        self.saved.append(secret)

    def delete(self) -> None:
        self.secret = None


class StubProvider:
    def __init__(self, *, config: EmbeddingConnectionConfig, api_key: str) -> None:
        self.config = config
        self.api_key = api_key

    def embed_query(self, _query: str) -> list[float]:
        return [0.25] * self.config.output_dimension


def test_embedding_defaults_and_index_identity_are_secret_free() -> None:
    first = EmbeddingConnectionConfig()
    changed = first.model_copy(update={"model": "voyage-4"})

    assert first.endpoint == DEFAULT_EMBEDDING_ENDPOINT
    assert first.model == DEFAULT_EMBEDDING_MODEL
    assert first.output_dimension == 1024
    assert first.index_id != changed.index_id
    assert first.model_id.startswith("voyage:voyage-code-4:1024:")
    assert "api" not in first.model_dump()


def test_embedding_store_saves_non_secret_config_and_preserves_blank_key(
    tmp_path: Path,
) -> None:
    secrets = FakeSecretStore("existing-voyage-secret")
    store = EmbeddingConfigStore(tmp_path, secrets)
    config = EmbeddingConnectionConfig(
        endpoint="https://embedding.example/v1/embeddings/",
        model="voyage-code-4",
        output_dimension=512,
    )

    state = store.save(config, "")
    content = store.path.read_text(encoding="utf-8")

    assert state.configured is True
    assert state.config is not None
    assert state.config.endpoint == "https://embedding.example/v1/embeddings"
    assert secrets.secret == "existing-voyage-secret"
    assert "existing-voyage-secret" not in content
    assert "api_key" not in content


def test_embedding_store_requires_a_key_for_first_save(tmp_path: Path) -> None:
    store = EmbeddingConfigStore(tmp_path, FakeSecretStore())

    with pytest.raises(EmbeddingSetupError, match="API Key"):
        store.save(EmbeddingConnectionConfig(), "")


def test_embedding_service_tests_unsaved_values_without_returning_secret(
    tmp_path: Path,
) -> None:
    secrets = FakeSecretStore()
    store = EmbeddingConfigStore(tmp_path, secrets)
    service = EmbeddingSetupService(
        Settings(data_dir=tmp_path),
        store,
        provider_factory=StubProvider,
    )
    config = service.build_config(
        endpoint="https://api.voyageai.com/v1/embeddings",
        model="voyage-code-4",
        output_dimension="256",
    )

    result = service.test(config, "temporary-secret")

    assert result == "连接成功 · voyage-code-4 · 256 维"
    assert secrets.secret is None
    assert "temporary-secret" not in result


@pytest.mark.parametrize(
    ("endpoint", "model", "dimension"),
    [
        ("voyage.example/v1/embeddings", "voyage-code-4", "1024"),
        ("https://voyage.example/v1/embeddings", " ", "1024"),
        ("https://voyage.example/v1/embeddings", "voyage-code-4", "not-a-number"),
    ],
)
def test_embedding_service_rejects_invalid_configuration(
    tmp_path: Path,
    endpoint: str,
    model: str,
    dimension: str,
) -> None:
    service = EmbeddingSetupService(
        Settings(data_dir=tmp_path),
        EmbeddingConfigStore(tmp_path, FakeSecretStore()),
    )

    with pytest.raises(EmbeddingSetupError, match="配置不完整"):
        service.build_config(
            endpoint=endpoint,
            model=model,
            output_dimension=dimension,
        )
