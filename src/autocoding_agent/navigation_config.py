"""入口 MD 中显式配置的目录 SQL 和路径规则；正文说明不作为可执行配置。"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from autocoding_agent.navigation_catalog import MenuCatalogConfig
from autocoding_agent.navigation_paths import PathMappingRule
from autocoding_agent.workspace_knowledge import KnowledgeDomain, MarkdownKnowledgeService


class NavigationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalogs: list[MenuCatalogConfig] = Field(
        default_factory=list, max_length=8, description="各菜单数据源的只读目录配置。"
    )
    path_mappings: list[PathMappingRule] = Field(
        default_factory=list, max_length=32, description="命名空间到项目内目录的显式映射。"
    )


def load_navigation_config(project: str, knowledge_root: Path) -> NavigationConfig:
    """只解析选中项目内单个命名 JSON 块；不存在时保持旧知识兼容。"""
    service = MarkdownKnowledgeService(knowledge_root.parent)
    path = service.branch_path(KnowledgeDomain.NAVIGATION, project)
    if not path.is_file():
        return NavigationConfig()
    content = path.read_text(encoding="utf-8")
    blocks = re.findall(r"^```ace-navigation[ \t]*\r?\n(.*?)^```[ \t]*$", content, re.M | re.S)
    if len(blocks) > 1:
        raise ValueError("入口知识中只能配置一个 ace-navigation 块")
    if not blocks:
        return NavigationConfig()
    if len(blocks[0]) > 64_000:
        raise ValueError("入口配置过大")
    try:
        return NavigationConfig.model_validate_json(blocks[0])
    except ValueError:
        raise ValueError("入口 ace-navigation 配置格式无效，请检查字段与 JSON") from None
