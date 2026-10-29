"""Oracle 连接服务，复用数据源配置的凭据管理及只读查询适配器。"""

from autocoding_agent.adapters.oracle_database import OracleDatabaseReader
from autocoding_agent.config import Settings
from autocoding_agent.oracle_config import OracleConfigStore, OracleConnectionConfig


class OracleConnectionService:
    def __init__(self, settings: Settings, store: OracleConfigStore) -> None:
        self.settings = settings
        self.store = store

    def inspect(self):
        return self.store.load()

    def save(self, config: OracleConnectionConfig, password: str = ""):
        return self.store.save(config, password)

    def test(self, config: OracleConnectionConfig, password: str = "") -> str:
        return self._reader(config, password or self.store.password_for(config)).test_connection()

    def reader(self):
        state = self.inspect()
        if not state.configured:
            return None
        return self._reader(state.config, self.store.password_for(state.config))

    def _reader(self, config: OracleConnectionConfig, password: str | None):
        return OracleDatabaseReader(
            config,
            password,
            max_rows=self.settings.database_max_rows,
            query_timeout_seconds=self.settings.database_query_timeout_seconds,
        )
