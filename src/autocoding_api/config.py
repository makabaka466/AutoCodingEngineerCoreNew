"""服务端项目白名单和调用者配置，不接受客户端指定本机路径。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace: Path = Field(description="服务器上的授权工作区绝对路径。")
    knowledge_project: str | None = Field(default=None, description="现有领域知识的项目名称。")


class UserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(description="此调用者的 Bearer Token，至少 32 个字符。")
    projects: list[str] = Field(min_length=1, description="此调用者允许访问的项目 ID。")


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Field(description="独立服务数据目录，API 和 Worker 必须使用同一路径。")
    projects: dict[str, ProjectConfig] = Field(min_length=1, description="服务器项目白名单。")
    users: dict[str, UserConfig] = Field(min_length=1, description="稳定用户 ID 与认证配置。")

    @model_validator(mode="after")
    def check_configuration(self) -> ServerConfig:
        if not self.data_dir.is_absolute():
            raise ValueError("data_dir 必须是绝对路径")
        self.data_dir = self.data_dir.resolve()
        tokens = set()
        for user in self.users.values():
            token = user.token.get_secret_value()
            if (
                len(token) < 32 or token.strip() != token or token in tokens
                or token.startswith("REPLACE_WITH_")
            ):
                raise ValueError("Token 必须唯一、至少 32 字符且没有前后空白")
            tokens.add(token)
            if not set(user.projects) <= self.projects.keys():
                raise ValueError("用户引用了未配置的项目")
        for project in self.projects.values():
            if not project.workspace.is_absolute() or not project.workspace.is_dir():
                raise ValueError("workspace 必须是存在的绝对目录")
            project.workspace = project.workspace.resolve()
        return self


def load_config(path: str | Path) -> ServerConfig:
    return ServerConfig.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
