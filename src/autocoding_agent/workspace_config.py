"""Persistent non-secret project workspace configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from autocoding_agent.config import Settings, get_settings


class WorkspaceConfigError(ValueError):
    """A safe validation or persistence error for the settings UI."""


class WorkspaceConfig(BaseModel):
    """The configured source repository used when a new desktop task starts."""

    model_config = ConfigDict(extra="forbid")

    path: str


class WorkspaceConfigState(BaseModel):
    config: WorkspaceConfig | None = None
    available: bool = False

    @property
    def configured(self) -> bool:
        return self.config is not None and self.available


class WorkspaceConfigStore:
    """Atomically store a validated workspace path outside the project repository."""

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir).expanduser().resolve() / "workspace" / "project.json"

    def load(self) -> WorkspaceConfigState:
        if not self.path.is_file():
            return WorkspaceConfigState()
        try:
            config = WorkspaceConfig.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise WorkspaceConfigError(f"项目路径配置文件无效：{exc}") from exc
        candidate = Path(config.path).expanduser()
        return WorkspaceConfigState(config=config, available=candidate.is_dir())

    def save(self, workspace: str | Path) -> WorkspaceConfigState:
        if not str(workspace).strip():
            raise WorkspaceConfigError("请先选择项目代码根目录。")
        try:
            canonical = Path(workspace).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceConfigError(f"项目路径不存在或无法访问：{exc}") from exc
        if not canonical.is_dir():
            raise WorkspaceConfigError("项目路径必须是一个可访问的目录。")
        config = WorkspaceConfig(path=str(canonical))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            raise WorkspaceConfigError(f"无法保存项目路径配置：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return WorkspaceConfigState(config=config, available=True)


class WorkspaceConfigService:
    """Small facade shared by the desktop client and system settings window."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: WorkspaceConfigStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or WorkspaceConfigStore(self.settings.data_dir)

    def inspect(self) -> WorkspaceConfigState:
        return self.store.load()

    def save(self, workspace: str | Path) -> WorkspaceConfigState:
        return self.store.save(workspace)
