"""Application service for saved SQL Server connections."""

from __future__ import annotations

from autocoding_agent.adapters.sqlserver_database import (
    SQLServerDatabaseReader,
    available_sqlserver_drivers,
)
from autocoding_agent.config import Settings, get_settings
from autocoding_agent.sqlserver_config import (
    SQLServerAuthentication,
    SQLServerConfigError,
    SQLServerConfigState,
    SQLServerConfigStore,
    SQLServerConnectionConfig,
)


class SQLServerConnectionService:
    """Coordinate validation, secret lookup, connection testing, and reader creation."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: SQLServerConfigStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or SQLServerConfigStore(self.settings.data_dir)

    def inspect(self) -> SQLServerConfigState:
        return self.store.load()

    def drivers(self) -> list[str]:
        return available_sqlserver_drivers()

    def save(
        self,
        config: SQLServerConnectionConfig,
        password: str = "",
    ) -> SQLServerConfigState:
        self._validate_driver(config)
        return self.store.save(config, password)

    def test(
        self,
        config: SQLServerConnectionConfig,
        password: str = "",
    ) -> str:
        self._validate_driver(config)
        effective_password = password or self.store.password_for(config)
        reader = self._reader(config, effective_password)
        return reader.test_connection()

    def reader(self) -> SQLServerDatabaseReader | None:
        state = self.store.load()
        if not state.configured or state.config is None:
            return None
        return self._reader(state.config, self.store.password_for(state.config))

    def _reader(
        self,
        config: SQLServerConnectionConfig,
        password: str | None,
    ) -> SQLServerDatabaseReader:
        return SQLServerDatabaseReader(
            config,
            password,
            max_rows=self.settings.database_max_rows,
            query_timeout_seconds=self.settings.database_query_timeout_seconds,
        )

    def _validate_driver(self, config: SQLServerConnectionConfig) -> None:
        drivers = self.drivers()
        if not drivers:
            raise SQLServerConfigError(
                "未检测到 Microsoft SQL Server ODBC 驱动，请先安装 ODBC Driver 17 或 18。"
            )
        if config.driver not in drivers:
            raise SQLServerConfigError(f"所选 ODBC 驱动未安装：{config.driver}")


def build_connection_config(
    *,
    server: str,
    port: str,
    database: str,
    driver: str,
    authentication: str,
    username: str,
    encrypt: bool,
    trust_server_certificate: bool,
) -> SQLServerConnectionConfig:
    """Convert string UI fields into the validated public contract."""

    try:
        parsed_port = int(port.strip())
    except ValueError as exc:
        raise SQLServerConfigError("端口必须是 1 到 65535 之间的数字。") from exc
    try:
        return SQLServerConnectionConfig(
            server=server,
            port=parsed_port,
            database=database,
            driver=driver,
            authentication=SQLServerAuthentication(authentication),
            username=username or None,
            encrypt=encrypt,
            trust_server_certificate=trust_server_certificate,
        )
    except ValueError as exc:
        raise SQLServerConfigError(f"连接配置不完整：{exc}") from exc
