"""Voyage Embedding configuration with OS-backed secret persistence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from autocoding_agent.config import Settings, get_settings
from autocoding_agent.knowledge_rag.voyage import VoyageEmbeddingProvider

DEFAULT_EMBEDDING_ENDPOINT = "https://api.voyageai.com/v1/embeddings"
DEFAULT_EMBEDDING_MODEL = "voyage-code-4"
DEFAULT_EMBEDDING_DIMENSION = 1024
SUPPORTED_EMBEDDING_DIMENSIONS = (256, 512, 1024, 2048)

_KEYRING_SERVICE = "AutoCodingEngineerCoreNew/Embedding"
_KEYRING_ACCOUNT = "voyage"


class EmbeddingSetupError(ValueError):
    """A safe configuration error suitable for display in the desktop UI."""


class EmbeddingSecretStore(Protocol):
    def get(self) -> str | None: ...

    def set(self, secret: str) -> None: ...

    def delete(self) -> None: ...


class KeyringEmbeddingSecretStore:
    """Keep Voyage API keys in the current user's OS credential manager."""

    def get(self) -> str | None:
        try:
            import keyring

            return keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except Exception as exc:
            raise EmbeddingSetupError(f"无法读取 Embedding 系统凭据：{exc}") from exc

    def set(self, secret: str) -> None:
        try:
            import keyring

            keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, secret)
        except Exception as exc:
            raise EmbeddingSetupError(f"无法保存 Embedding 系统凭据：{exc}") from exc

    def delete(self) -> None:
        try:
            import keyring
            from keyring.errors import PasswordDeleteError

            try:
                keyring.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
            except PasswordDeleteError:
                pass
        except Exception as exc:
            raise EmbeddingSetupError(f"无法清除 Embedding 系统凭据：{exc}") from exc


class EmbeddingConnectionConfig(BaseModel):
    """Secret-free provider settings; the source Markdown remains provider-neutral."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "voyage"
    endpoint: str = DEFAULT_EMBEDDING_ENDPOINT
    model: str = DEFAULT_EMBEDDING_MODEL
    output_dimension: int = Field(default=DEFAULT_EMBEDDING_DIMENSION, ge=1, le=4096)
    request_timeout_seconds: int = Field(default=30, ge=1, le=60)

    @field_validator("provider", "model")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value cannot be empty")
        return cleaned

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        if value.casefold() != "voyage":
            raise ValueError("only the voyage provider is supported")
        return "voyage"

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        parsed = urlparse(cleaned)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("endpoint must be a complete HTTP(S) URL without credentials")
        return cleaned

    @property
    def index_id(self) -> str:
        identity = (
            f"{self.provider}\n{self.endpoint.casefold()}\n{self.model}\n"
            f"{self.output_dimension}"
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    @property
    def model_id(self) -> str:
        return f"voyage:{self.model}:{self.output_dimension}:{self.index_id[:8]}"


class EmbeddingSetupState(BaseModel):
    config: EmbeddingConnectionConfig | None = None
    has_api_key: bool = False

    @property
    def configured(self) -> bool:
        return self.config is not None and self.has_api_key


class EmbeddingConfigStore:
    """Atomically save non-secrets and delegate the API key to the OS."""

    def __init__(
        self,
        data_dir: str | Path,
        secrets: EmbeddingSecretStore | None = None,
    ) -> None:
        self.path = Path(data_dir).expanduser().resolve() / "embedding" / "voyage.json"
        self.secrets = secrets or KeyringEmbeddingSecretStore()

    def load(self) -> EmbeddingSetupState:
        config: EmbeddingConnectionConfig | None = None
        if self.path.is_file():
            try:
                config = EmbeddingConnectionConfig.model_validate_json(
                    self.path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise EmbeddingSetupError(f"Embedding 配置文件无效：{exc}") from exc
        return EmbeddingSetupState(
            config=config,
            has_api_key=bool(self.secrets.get()) if config is not None else False,
        )

    def save(
        self,
        config: EmbeddingConnectionConfig,
        api_key: str = "",
    ) -> EmbeddingSetupState:
        previous_secret = self.secrets.get()
        clean_secret = api_key.strip()
        if not clean_secret and not previous_secret:
            raise EmbeddingSetupError("请输入 Embedding API Key。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if clean_secret:
                self.secrets.set(clean_secret)
            os.replace(temporary, self.path)
        except Exception:
            try:
                if previous_secret is None:
                    self.secrets.delete()
                else:
                    self.secrets.set(previous_secret)
            except Exception:
                pass
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return self.load()

    def api_key(self) -> str | None:
        return self.secrets.get()


class EmbeddingSetupService:
    """Inspect, test, save, and construct the configured Voyage provider."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: EmbeddingConfigStore | None = None,
        provider_factory=VoyageEmbeddingProvider,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or EmbeddingConfigStore(self.settings.data_dir)
        self.provider_factory = provider_factory

    def inspect(self) -> EmbeddingSetupState:
        return self.store.load()

    def defaults(self) -> EmbeddingConnectionConfig:
        return EmbeddingConnectionConfig()

    def build_config(
        self,
        *,
        endpoint: str,
        model: str,
        output_dimension: int | str,
    ) -> EmbeddingConnectionConfig:
        try:
            return EmbeddingConnectionConfig(
                endpoint=endpoint,
                model=model,
                output_dimension=int(output_dimension),
            )
        except (TypeError, ValueError) as exc:
            raise EmbeddingSetupError(f"Embedding 配置不完整：{exc}") from exc

    def save(
        self,
        config: EmbeddingConnectionConfig,
        api_key: str = "",
    ) -> EmbeddingSetupState:
        return self.store.save(config, api_key)

    def test(
        self,
        config: EmbeddingConnectionConfig,
        api_key: str = "",
    ) -> str:
        secret = api_key.strip() or self.store.api_key()
        if not secret:
            raise EmbeddingSetupError("请输入 Embedding API Key 后再测试连接。")
        provider = self.provider_factory(config=config, api_key=secret)
        vector = provider.embed_query("AutoCoding Engineer Voyage connection test")
        if len(vector) != config.output_dimension:
            raise EmbeddingSetupError(
                f"Embedding 返回维度 {len(vector)}，与配置 {config.output_dimension} 不一致。"
            )
        return f"连接成功 · {config.model} · {len(vector)} 维"

    def provider(self) -> VoyageEmbeddingProvider | None:
        state = self.inspect()
        if not state.configured or state.config is None:
            return None
        secret = self.store.api_key()
        if not secret:
            return None
        return self.provider_factory(config=state.config, api_key=secret)
