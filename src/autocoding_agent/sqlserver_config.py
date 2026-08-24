"""Persistent SQL Server connection settings with OS-backed secret storage."""

from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

_KEYRING_SERVICE = "AutoCodingEngineerCoreNew/SQLServer"
_KEYRING_ACCOUNT = "default-connection"


class SQLServerConfigError(ValueError):
    """A safe validation or credential-storage error for the desktop UI."""


class SQLServerAuthentication(StrEnum):
    WINDOWS = "windows"
    SQL_PASSWORD = "sql_password"


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SQLServerConnectionConfig(BaseModel):
    """Non-secret SQL Server connection settings."""

    model_config = ConfigDict(extra="forbid")

    server: NonEmptyText
    port: int = Field(default=1433, ge=1, le=65535)
    database: NonEmptyText
    driver: NonEmptyText = "ODBC Driver 17 for SQL Server"
    authentication: SQLServerAuthentication = SQLServerAuthentication.WINDOWS
    username: str | None = None
    encrypt: bool = True
    trust_server_certificate: bool = False
    connection_timeout_seconds: int = Field(default=10, ge=1, le=60)

    @field_validator("server", "database", "driver", "username")
    @classmethod
    def reject_connection_string_delimiters(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if any(character in cleaned for character in ";{}\x00\r\n"):
            raise ValueError("connection fields cannot contain ;, braces, nulls, or newlines")
        return cleaned or None

    @property
    def reference(self) -> str:
        """Return a safe session reference that contains no credentials."""

        return f"sqlserver://{self.server}:{self.port}/{self.database}"


class SecretStore(Protocol):
    def get(self) -> str | None: ...

    def set(self, password: str) -> None: ...

    def delete(self) -> None: ...


class KeyringSecretStore:
    """Use Windows Credential Manager through the standard keyring package."""

    def get(self) -> str | None:
        try:
            import keyring

            return keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except Exception as exc:
            raise SQLServerConfigError(f"无法读取系统凭据：{exc}") from exc

    def set(self, password: str) -> None:
        try:
            import keyring

            keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, password)
        except Exception as exc:
            raise SQLServerConfigError(f"无法保存到系统凭据管理器：{exc}") from exc

    def delete(self) -> None:
        try:
            import keyring
            from keyring.errors import PasswordDeleteError

            try:
                keyring.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
            except PasswordDeleteError:
                pass
        except Exception as exc:
            raise SQLServerConfigError(f"无法清除系统凭据：{exc}") from exc


class SQLServerConfigState(BaseModel):
    config: SQLServerConnectionConfig | None = None
    has_password: bool = False

    @property
    def configured(self) -> bool:
        if self.config is None:
            return False
        return (
            self.config.authentication == SQLServerAuthentication.WINDOWS
            or self.has_password
        )


class SQLServerConfigStore:
    """Atomically store non-secret settings and delegate the password to the OS."""

    def __init__(self, data_dir: str | Path, secrets: SecretStore | None = None) -> None:
        self.path = Path(data_dir).expanduser().resolve() / "database" / "sqlserver.json"
        self.secrets = secrets or KeyringSecretStore()

    def load(self) -> SQLServerConfigState:
        config: SQLServerConnectionConfig | None = None
        if self.path.is_file():
            try:
                config = SQLServerConnectionConfig.model_validate_json(
                    self.path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise SQLServerConfigError(f"SQL Server 配置文件无效：{exc}") from exc
        has_password = bool(self.secrets.get()) if config is not None else False
        return SQLServerConfigState(config=config, has_password=has_password)

    def save(
        self,
        config: SQLServerConnectionConfig,
        password: str = "",
    ) -> SQLServerConfigState:
        self._validate_authentication(config, password)
        previous_password = self.secrets.get()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if config.authentication == SQLServerAuthentication.SQL_PASSWORD and password:
                self.secrets.set(password)
            elif config.authentication == SQLServerAuthentication.WINDOWS:
                self.secrets.delete()
            os.replace(temporary, self.path)
        except Exception:
            # Keep an older saved configuration usable if replacing its JSON fails after
            # the OS credential was updated.
            try:
                if previous_password is None:
                    self.secrets.delete()
                else:
                    self.secrets.set(previous_password)
            except Exception:
                pass
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return self.load()

    def password_for(self, config: SQLServerConnectionConfig) -> str | None:
        if config.authentication == SQLServerAuthentication.WINDOWS:
            return None
        return self.secrets.get()

    def _validate_authentication(
        self,
        config: SQLServerConnectionConfig,
        password: str,
    ) -> None:
        if config.authentication == SQLServerAuthentication.WINDOWS:
            return
        if not config.username:
            raise SQLServerConfigError("SQL Server 认证需要填写用户名。")
        if not password and not self.secrets.get():
            raise SQLServerConfigError("SQL Server 认证需要填写密码。")
