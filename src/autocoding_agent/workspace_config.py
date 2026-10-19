"""Persistent non-secret project workspace configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from autocoding_agent.config import Settings, get_settings
from autocoding_agent.git_version import GitTarget, GitVersionError, GitVersionService
from autocoding_agent.workspace_knowledge import MarkdownKnowledgeService


class WorkspaceConfigError(ValueError):
    """A safe validation or persistence error for the settings UI."""


class WorkspaceConfig(BaseModel):
    """The configured source repository used when a new desktop task starts."""

    model_config = ConfigDict(extra="forbid")

    path: str
    git_remote: str | None = None
    git_branch: str | None = None

    @model_validator(mode="after")
    def complete_git_target(self):
        if bool(self.git_remote) != bool(self.git_branch):
            raise ValueError("Git 远端地址和目标分支必须同时填写")
        return self


class WorkspaceConfigState(BaseModel):
    config: WorkspaceConfig | None = None
    available: bool = False

    @property
    def configured(self) -> bool:
        return self.config is not None and self.available


class WorkspaceProject(WorkspaceConfig):
    """项目身份与路径分离；历史会话始终保留创建时的执行目录。"""

    id: str = Field(
        pattern=r"^ace-[a-zA-Z0-9_-]+$",
        description="稳定项目标识，同时用作两份知识文件的目录名",
    )
    name: str = Field(description="界面展示名称，允许修改")
    previous_paths: list[str] = Field(default_factory=list, description="旧会话归属匹配路径")
    migrated: bool = Field(default=False, description="是否来自旧版单工作区配置")

    def owns(self, project: str | None, workspace: str) -> bool:
        if project == self.id:
            return True
        # 新项目 ID 不按目录兜底；仅接收旧版未使用稳定 ID 的会话。
        if project and project.startswith("ace-"):
            return False
        return Path(workspace).resolve() in {
            Path(path).resolve() for path in [self.path, *self.previous_paths]
        }


class WorkspaceConfigStore:
    """Atomically store a validated workspace path outside the project repository."""

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir).expanduser().resolve() / "workspace" / "project.json"

    def load(self) -> WorkspaceConfigState:
        if self.path.with_name("projects.json").is_file():
            projects = self.list_projects()
            config = projects[0] if projects else None
            return WorkspaceConfigState(
                config=config, available=bool(config and Path(config.path).is_dir()),
            )
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

    def save(
        self, workspace: str | Path, git_remote: str | None = None,
        git_branch: str | None = None,
    ) -> WorkspaceConfigState:
        if self.path.with_name("projects.json").is_file():
            projects = self.list_projects()
            current = projects[0] if projects else None
            project = self.save_project(
                current.name if current else Path(workspace).name, workspace,
                git_remote, git_branch, project_id=current.id if current else None,
            )
            return WorkspaceConfigState(config=project, available=True)
        config = self._validated_config(workspace, git_remote, git_branch)
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

    def list_projects(self) -> list[WorkspaceProject]:
        catalog = self.path.with_name("projects.json")
        if catalog.is_file():
            try:
                return TypeAdapter(list[WorkspaceProject]).validate_json(
                    catalog.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise WorkspaceConfigError(f"项目配置文件无效：{exc}") from exc
        legacy = self.load().config
        if legacy is None:
            return []
        projects = [WorkspaceProject(
            **legacy.model_dump(), id=f"ace-{uuid4().hex}",
            name=Path(legacy.path).name or "默认项目", migrated=True,
        )]
        self._save_projects(projects)
        return projects

    def save_project(
        self, name: str, workspace: str | Path, git_remote: str | None = None,
        git_branch: str | None = None, *, project_id: str | None = None,
    ) -> WorkspaceProject:
        name = name.strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise WorkspaceConfigError("项目名称须为 1–80 个字符，不能包含控制字符。")
        config = self._validated_config(workspace, git_remote, git_branch)
        canonical = Path(config.path)
        projects = self.list_projects()
        current = next((item for item in projects if item.id == project_id), None)
        if project_id and current is None:
            raise WorkspaceConfigError("待修改的项目不存在，请刷新配置。")
        for item in projects:
            if item.id == project_id:
                continue
            if item.name.casefold() == name.casefold():
                raise WorkspaceConfigError("项目名称已存在。")
            if canonical in {Path(p).resolve() for p in [item.path, *item.previous_paths]}:
                raise WorkspaceConfigError("该路径已归属其他项目，不能重复绑定。")
        previous = list(current.previous_paths) if current else []
        if current and current.path != str(canonical) and current.path not in previous:
            previous.append(current.path)
        project = WorkspaceProject(
            id=current.id if current else f"ace-{uuid4().hex}", name=name,
            **config.model_dump(),
            previous_paths=previous, migrated=current.migrated if current else False,
        )
        projects = [project if item.id == project.id else item for item in projects]
        if current is None:
            projects.append(project)
        self._save_projects(projects)
        return project

    @staticmethod
    def _validated_config(
        workspace: str | Path, git_remote: str | None, git_branch: str | None,
    ) -> WorkspaceConfig:
        """旧接口与项目维护共用目录和 Git 校验，保持原有调用契约。"""
        if not str(workspace).strip():
            raise WorkspaceConfigError("请先选择项目代码根目录。")
        try:
            canonical = Path(workspace).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceConfigError(f"项目路径不存在或无法访问：{exc}") from exc
        if not canonical.is_dir():
            raise WorkspaceConfigError("项目路径必须是一个可访问的目录。")
        config = WorkspaceConfig(
            path=str(canonical), git_remote=git_remote or None,
            git_branch=git_branch or None,
        )
        if config.git_remote:
            try:
                GitVersionService(GitTarget(
                    canonical, config.git_remote, config.git_branch or "",
                ))
            except GitVersionError as exc:
                raise WorkspaceConfigError(str(exc)) from exc
        return config

    def _save_projects(self, projects: list[WorkspaceProject]) -> None:
        catalog = self.path.with_name("projects.json")
        catalog.parent.mkdir(parents=True, exist_ok=True)
        temporary = catalog.with_name(f".{catalog.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps([p.model_dump() for p in projects], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, catalog)
        finally:
            temporary.unlink(missing_ok=True)


class WorkspaceConfigService:
    """Small facade shared by the desktop client and system settings window."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: WorkspaceConfigStore | None = None,
        knowledge: MarkdownKnowledgeService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or WorkspaceConfigStore(self.settings.data_dir)
        self.knowledge = knowledge or MarkdownKnowledgeService()

    def list_projects(self) -> list[WorkspaceProject]:
        projects = self.store.list_projects()
        for project in projects:
            self.knowledge.ensure_project(
                project.id, legacy_name=project.name if project.migrated else None,
            )
        return projects

    def save_project(
        self, name: str, workspace: str | Path, git_remote: str | None = None,
        git_branch: str | None = None, *, project_id: str | None = None,
    ) -> WorkspaceProject:
        project = self.store.save_project(
            name, workspace, git_remote, git_branch, project_id=project_id,
        )
        self.knowledge.ensure_project(project.id)
        return project

    def inspect(self) -> WorkspaceConfigState:
        return self.store.load()

    def save(
        self, workspace: str | Path, git_remote: str | None = None,
        git_branch: str | None = None,
    ) -> WorkspaceConfigState:
        return self.store.save(workspace, git_remote, git_branch)
