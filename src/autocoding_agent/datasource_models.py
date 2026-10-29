"""项目数据源和任务绑定的无凭据数据契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from autocoding_agent.database_models import QuerySource


class DataSourceDefinition(QuerySource):
    """非秘密元数据；连接参数保存在独立连接文件中。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    database_type: Literal["sqlserver", "oracle"] = Field(
        default="sqlserver", description="数据库方言",
    )
    datasource_id: str = Field(pattern=r"^ds-[a-zA-Z0-9_-]+$", description="稳定数据源 ID")
    name: str = Field(min_length=1, max_length=100, description="展示名称")
    environment: str = Field(min_length=1, max_length=100, description="用户确认的环境标签")
    subproject: str = Field(default="", max_length=200, description="所属子项目说明")
    purpose: str = Field(default="", max_length=300, description="菜单、业务、日志等用途说明")


class ProjectDataSources(BaseModel):
    """项目授权范围；默认值只用于模型未显式指定数据源时。"""

    model_config = ConfigDict(extra="forbid")
    source_ids: list[str] = Field(default_factory=list, description="允许查询的数据源 ID")
    menu_default: str | None = Field(default=None, description="菜单查询默认数据源")
    business_default: str | None = Field(default=None, description="业务查询默认数据源")

    @model_validator(mode="after")
    def check_defaults(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("数据源绑定不能重复")
        for value in (self.menu_default, self.business_default):
            if value is not None and value not in self.source_ids:
                raise ValueError("默认数据源必须属于项目绑定范围")
        return self


class DataSourceCatalog(BaseModel):
    sources: dict[str, DataSourceDefinition] = Field(default_factory=dict)
    projects: dict[str, ProjectDataSources] = Field(default_factory=dict)
    connection_fingerprints: dict[str, str] = Field(
        default_factory=dict, description="与元信息同时提交的连接指纹，拒绝读取半保存配置",
    )

    @model_validator(mode="after")
    def check_references(self):
        if any(key != value.datasource_id for key, value in self.sources.items()):
            raise ValueError("数据源 ID 与目录键不一致")
        for binding in self.projects.values():
            if not set(binding.source_ids).issubset(self.sources):
                raise ValueError("项目绑定了不存在的数据源")
        return self


class TaskDataSources(BaseModel):
    """任务创建时冻结的授权、默认值与配置指纹；没有密码或连接字符串。"""

    project: str = Field(description="任务所属项目 ID")
    binding: ProjectDataSources
    sources: dict[str, DataSourceDefinition] = Field(default_factory=dict)
    fingerprints: dict[str, str] = Field(default_factory=dict)
