"""项目数据源目录与任务只读路由；连接凭据继续由系统凭据管理器保管。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from autocoding_agent.config import Settings
from autocoding_agent.database_models import DataQuery, QueryResult, QuerySource
from autocoding_agent.database_prompt import compact_database_context
from autocoding_agent.datasource_models import (
    DataSourceCatalog,
    DataSourceDefinition,
    ProjectDataSources,
    TaskDataSources,
)
from autocoding_agent.oracle_config import OracleConfigStore, OracleConnectionConfig
from autocoding_agent.oracle_service import OracleConnectionService
from autocoding_agent.sqlserver_config import (
    KeyringSecretStore,
    SQLServerAuthentication,
    SQLServerConfigStore,
    SQLServerConnectionConfig,
)
from autocoding_agent.sqlserver_service import SQLServerConnectionService
from autocoding_agent.workspace_knowledge import PROJECT_KNOWLEDGE_ROOT


def query_source(session, query: DataQuery) -> QuerySource | None:
    """失败查询也保留尝试使用的来源；未授权的 ID 不伪造来源记录。"""
    snapshot = session.database_binding
    source = snapshot.sources.get(query.datasource_id) if snapshot is not None else None
    return QuerySource.model_validate(source.model_dump()) if source is not None else None


class DataSourceService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.data_dir / "database" / "datasources.json"

    def load(self) -> DataSourceCatalog:
        if not self.path.exists():
            return DataSourceCatalog()
        return DataSourceCatalog.model_validate_json(self.path.read_text(encoding="utf-8"))

    def _save(self, catalog: DataSourceCatalog) -> None:
        # 先完整校验，再原子替换，避免断电留下半份 JSON。
        catalog = DataSourceCatalog.model_validate(catalog.model_dump())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(catalog.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def connection(self, source_id: str, database_type: str | None = None):
        # 先验证 ID，不能让外部输入拼接成任意文件路径或凭据名。
        if not source_id.startswith("ds-") or not all(
            c.isascii() and (c.isalnum() or c in "-_") for c in source_id
        ):
            raise ValueError("无效数据源 ID")
        if database_type is None:
            definition = self.load().sources.get(source_id)
            database_type = definition.database_type if definition else "sqlserver"
        if database_type not in {"sqlserver", "oracle"}:
            raise ValueError("不支持的数据源类型")
        store_type = OracleConfigStore if database_type == "oracle" else SQLServerConfigStore
        store = store_type(
            self.settings.data_dir, secrets=KeyringSecretStore(source_id),
        )
        store.path = self.path.parent / "connections" / f"{source_id}.json"
        service_type = (
            OracleConnectionService if database_type == "oracle" else SQLServerConnectionService
        )
        return service_type(self.settings, store)

    def save_source(
        self, definition: DataSourceDefinition,
        config: SQLServerConnectionConfig | OracleConnectionConfig,
        password: str = "",
    ) -> None:
        expected_type = (
            OracleConnectionConfig if definition.database_type == "oracle"
            else SQLServerConnectionConfig
        )
        if not isinstance(config, expected_type):
            raise ValueError("连接参数与数据库类型不一致")
        catalog = self.load()
        old = catalog.sources.get(definition.datasource_id)
        if old is not None and old.database_type != definition.database_type:
            raise ValueError("已有数据源不能改变数据库类型，请新增数据源")
        self.connection(definition.datasource_id, definition.database_type).save(config, password)
        catalog.sources[definition.datasource_id] = definition
        catalog.connection_fingerprints[definition.datasource_id] = hashlib.sha256(
            config.model_dump_json().encode(),
        ).hexdigest()
        self._save(catalog)

    def bind_project(self, project: str, binding: ProjectDataSources) -> None:
        if not project.strip():
            raise ValueError("请先保存并选择项目")
        catalog = self.load()
        catalog.projects[project] = binding
        self._save(catalog)

    def snapshot(self, project: str) -> TaskDataSources | None:
        # 没有启用目录时兼容旧单连接；启用后，新任务不得回落到全局连接。
        if not self.path.exists():
            return None
        catalog = self.load()
        binding = catalog.projects.get(project, ProjectDataSources())
        sources = {key: catalog.sources[key] for key in binding.source_ids}
        return TaskDataSources(
            project=project, binding=binding, sources=sources,
            fingerprints={
                key: self._configuration(value, catalog.connection_fingerprints.get(key))[1]
                for key, value in sources.items()
            },
        )

    def _configuration(
        self, source: DataSourceDefinition, expected: str | None,
    ) -> tuple[SQLServerConnectionConfig | OracleConnectionConfig, str]:
        # 不读取密码；包含连接目标、Schema、环境及用途，配置变化不能静默影响旧任务。
        connection = self.connection(source.datasource_id, source.database_type)
        path = connection.store.path
        if not path.is_file():
            raise ValueError("数据源连接配置不存在，请重新保存")
        config = connection.store.config_model.model_validate_json(path.read_text(encoding="utf-8"))
        if hashlib.sha256(config.model_dump_json().encode()).hexdigest() != expected:
            raise ValueError("数据源连接与目录不一致，可能保存中断，请重新保存该数据源")
        payload = {"source": source.model_dump(), "connection": config.model_dump(mode="json")}
        return config, hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def execute(
        self, snapshot: TaskDataSources, query: DataQuery, stage: str | None,
    ) -> QueryResult:
        catalog = self.load()
        current = catalog.projects.get(snapshot.project, ProjectDataSources())
        if current != snapshot.binding:
            raise ValueError("任务的数据源授权或默认值已变化，请核对配置后新建任务")
        source_id = query.datasource_id
        if source_id is None:
            source_id = (
                snapshot.binding.menu_default if stage == "page_lookup"
                else snapshot.binding.business_default
            )
            if source_id is None and len(snapshot.binding.source_ids) == 1:
                source_id = snapshot.binding.source_ids[0]
        if source_id is None:
            raise ValueError("数据源不明确：请依据子项目和环境指定 datasource_id，必要时询问用户")
        if source_id not in snapshot.binding.source_ids:
            raise ValueError("查询数据源不属于当前任务绑定的项目")
        source = catalog.sources.get(source_id)
        if source is None:
            raise ValueError("任务绑定的数据源已被移除")
        config, fingerprint = self._configuration(
            source, catalog.connection_fingerprints.get(source_id),
        )
        if fingerprint != snapshot.fingerprints.get(source_id):
            raise ValueError("任务绑定的数据源配置已变化，请核对环境后新建任务")
        query.datasource_id = source_id
        connection = self.connection(source_id, source.database_type)
        password = connection.store.password_for(config)
        if config.authentication == SQLServerAuthentication.SQL_PASSWORD and not password:
            raise ValueError("数据源凭据不可用，请检查当前运行账户的连接配置")
        # 使用刚刚核验过的配置对象，不能重新读取文件后意外连接到另一个目标。
        reader = connection._reader(config, password)
        result = reader.execute(query)
        result.source = QuerySource.model_validate(source.model_dump())
        return result


class TaskDatabaseAccess:
    """两个工作流共享的入口；不在引擎全局切换连接，避免任务之间串库。"""

    def __init__(self, service, legacy, reference, *, knowledge_root: Path | None = None) -> None:
        self.service: DataSourceService | None = service
        self.legacy = legacy
        self.reference = reference
        self.knowledge_root = knowledge_root or PROJECT_KNOWLEDGE_ROOT

    def bind(self, session) -> None:
        if self.service is not None:
            session.database_binding = self.service.snapshot(session.project or "")
        if session.database_binding is not None:
            session.database_reference = None

    def available(self, session) -> bool:
        if session.database_binding is not None:
            return bool(session.database_binding.binding.source_ids)
        return self.legacy is not None

    def context(self, session) -> str:
        snapshot = session.database_binding
        if snapshot is None:
            return compact_database_context(
                configured=self.legacy is not None, reference=self.reference,
            )
        return (
            "Only these project-bound read-only datasources are authorized. Select datasource_id "
            "using subproject, purpose and the user's environment; ask when ambiguous. "
            "Environment labels are user configuration, not independently verified deployment. "
            "Use each source's database_type: sqlserver means T-SQL (TOP, sys/INFORMATION_SCHEMA); "
            "oracle means Oracle SQL (ROWNUM, ALL_TAB_COLUMNS/USER_TAB_COLUMNS, dual). "
            "Never mix dialects. Oracle uses :name binds, not @name or ?. "
            "Use parameterized :name values. Qualify tables with the "
            "configured schema after checking source code. "
            "Schema is context, not a security boundary. "
            "Menu and business evidence may use different sources. Defaults apply only when no "
            "datasource_id is supplied. Never infer business DB from menu DB.\n"
            + json.dumps({
                "sources": [source.model_dump() for source in snapshot.sources.values()],
                "menu_default": snapshot.binding.menu_default,
                "business_default": snapshot.binding.business_default,
            }, ensure_ascii=False)
        )

    def execute(
        self, session, query: DataQuery, stage: str | None = None, *, stop_requested=None,
    ) -> QueryResult:
        if query.query_kind == "menu_catalog":
            if stage not in {None, "page_lookup"}:
                raise ValueError("目录请求必须使用 page_lookup，不能代替业务数据查询")
            return self._menu_catalog(session, query, stop_requested)
        if session.database_binding is not None:
            if self.service is None:
                raise ValueError("项目数据源服务不可用")
            return self.service.execute(session.database_binding, query, stage)
        if self.legacy is None:
            raise ValueError("当前任务未配置只读数据源")
        if query.datasource_id is not None:
            raise ValueError("旧单连接任务不能选择项目数据源，请新建任务")
        if session.database_reference != self.reference:
            raise ValueError("任务原有数据库连接已变化，请恢复原配置或新建任务")
        return self.legacy.execute(query)


    def _menu_catalog(self, session, query: DataQuery, stop_requested) -> QueryResult:
        """宿主加载当前项目显式配置；目录只给候选，不替模型决定入口。"""
        from autocoding_agent.navigation_catalog import MenuCatalogService
        from autocoding_agent.navigation_config import load_navigation_config
        from autocoding_agent.navigation_paths import resolve_navigation_path

        snapshot = session.database_binding
        if self.service is None or snapshot is None or not session.project:
            raise ValueError("完整目录需要项目绑定的数据源，请配置后新建任务")
        source_id = query.datasource_id or snapshot.binding.menu_default
        if source_id is None:
            raise ValueError("请指定菜单数据源或设置项目菜单默认库")
        if source_id not in snapshot.binding.source_ids:
            raise ValueError("目录数据源不属于当前项目")
        configured = load_navigation_config(session.project, self.knowledge_root)
        matches = [item for item in configured.catalogs if item.datasource_id == source_id
                   or (item.datasource_id == "menu_default"
                       and source_id == snapshot.binding.menu_default)]
        if len(matches) != 1:
            raise ValueError("请在项目入口 MD 的 ace-navigation 中配置唯一匹配的目录 SQL")
        config = matches[0].model_copy(update={"datasource_id": source_id})
        query.datasource_id = source_id
        catalog = MenuCatalogService(self.service.settings.data_dir).load(
            session, config, self, refresh=query.refresh_catalog, stop_requested=stop_requested,
        )
        rows = []
        for row in catalog.rows:
            resolved = resolve_navigation_path(
                session.workspace, row["menu_path"], configured.path_mappings,
            )
            rows.append({**row, "candidate_paths": resolved.candidate_paths,
                         "path_notes": resolved.conflicts or resolved.missing})
        return QueryResult(
            query_name=query.name,
            source=QuerySource.model_validate(snapshot.sources[source_id].model_dump()),
            columns=["menu_id", "menu_name", "menu_path", "candidate_paths", "path_notes"],
            rows=rows, returned_rows=len(rows), truncated=not catalog.complete,
            catalog_info=catalog.model_dump(mode="json", exclude={"rows"}),
        )
