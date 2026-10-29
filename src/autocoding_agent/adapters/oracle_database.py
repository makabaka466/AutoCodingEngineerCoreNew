"""Oracle 有界只读查询；不自动改用另一模式、库或账号。"""

from __future__ import annotations

import re
from contextlib import closing
from pathlib import Path
from threading import Lock

import oracledb

from autocoding_agent.adapters.database_safety import (
    ReadOnlyQueryError,
    safe_value,
    sensitive_columns,
    unique_column_names,
    validate_read_only_sql,
)
from autocoding_agent.database_models import (
    DEFAULT_QUERY_MAX_ROWS,
    DEFAULT_QUERY_TIMEOUT_SECONDS,
    DataQuery,
    QueryResult,
)
from autocoding_agent.oracle_config import OracleConnectionConfig

_MODE_LOCK = Lock()
_MODE_SIGNATURE: tuple[str, str] | None = None


class OracleDatabaseReader:
    def __init__(
        self,
        config: OracleConnectionConfig,
        password: str | None,
        *,
        max_rows: int = DEFAULT_QUERY_MAX_ROWS,
        query_timeout_seconds: int = DEFAULT_QUERY_TIMEOUT_SECONDS,
    ) -> None:
        if not password:
            raise ValueError("Oracle 连接需要密码，请检查当前运行账户的凭据")
        if not 1 <= max_rows <= 1000 or not 1 <= query_timeout_seconds <= 60:
            raise ValueError("查询行数或超时超出允许范围")
        self.config = config
        self._password = password
        self.max_rows = max_rows
        self.query_timeout_seconds = query_timeout_seconds

    @property
    def reference(self) -> str:
        return self.config.reference

    def _connect(self):
        global _MODE_SIGNATURE
        directory = (
            str(Path(self.config.client_lib_dir).resolve()) if self.config.client_lib_dir else ""
        )
        signature = (self.config.mode, directory)
        try:
            # python-oracledb 模式属于进程；并发测试/任务不得抢先改变另一个连接的模式。
            with _MODE_LOCK:
                if _MODE_SIGNATURE is not None and _MODE_SIGNATURE != signature:
                    raise ReadOnlyQueryError(
                        "Oracle 模式或客户端目录与当前进程不同，请统一配置后重启 ACE"
                    )
                if _MODE_SIGNATURE is None:
                    if self.config.mode == "thin":
                        oracledb.enable_thin_mode()
                    else:
                        oracledb.init_oracle_client(lib_dir=directory or None)
                        if oracledb.clientversion()[0] < 19:
                            raise ReadOnlyQueryError(
                                "ACE 的 Thick 模式要求 Oracle Client 19 或更新版本"
                            )
                    _MODE_SIGNATURE = signature
            params = oracledb.ConnectParams(
                host=self.config.server,
                port=self.config.port,
                tcp_connect_timeout=self.config.connection_timeout_seconds,
                retry_count=0,
                **{self.config.identifier_type: self.config.database},
            )
            return oracledb.connect(
                user=self.config.username,
                password=self._password,
                params=params,
            )
        except oracledb.Error as exc:
            raise ReadOnlyQueryError(self._safe_error("Oracle 连接失败", exc)) from None

    def test_connection(self) -> str:
        """只读取连接身份和驱动报告的服务端版本，不访问业务表。"""
        try:
            with closing(self._connect()) as connection:
                connection.call_timeout = self.query_timeout_seconds * 1000
                with closing(connection.cursor()) as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        "SELECT SYS_CONTEXT('USERENV','DB_NAME'), "
                        "SYS_CONTEXT('USERENV','SERVICE_NAME'), "
                        "SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM dual"
                    )
                    row = cursor.fetchone()
                return (
                    f"连接成功 · Oracle {connection.version} · {self.config.mode} · "
                    f"DB={row[0]} / Service={row[1]} / Schema={row[2]}"
                )
        except oracledb.Error as exc:
            raise ReadOnlyQueryError(self._safe_error("Oracle 连接测试失败", exc)) from None

    def describe_schema(self) -> str:
        # 与业务查询共用限制及审计边界；默认只列登录用户拥有的元数据。
        result = self.execute(
            DataQuery(
                name="schema",
                purpose="检查 Oracle 表字段",
                max_rows=100,
                sql=(
                    "SELECT * FROM (SELECT table_name, column_name, data_type "
                    "FROM user_tab_columns ORDER BY table_name, column_id) WHERE ROWNUM <= 100"
                ),
            )
        )
        return "Database dialect: Oracle SQL.\n" + "\n".join(
            " | ".join(str(value) for value in row.values()) for row in result.rows
        )

    def execute(self, query: DataQuery) -> QueryResult:
        validate_read_only_sql(query.sql)
        # SELECT 也可能推进序列或调用有副作用的包；这里保守拒绝这些明显入口和跨库链接。
        if re.search(r"\b(?:nextval|currval|dbms_\w*|utl_\w*)\b|@", query.sql, re.I):
            raise ReadOnlyQueryError("Oracle 只读查询不允许序列、系统过程包或数据库链接")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in query.parameters):
            raise ReadOnlyQueryError("Oracle 参数名必须使用普通命名标识符")
        parameters = {name.upper(): value for name, value in query.parameters.items()}
        if any(isinstance(value, bool) for value in parameters.values()):
            raise ReadOnlyQueryError(
                "Oracle 19c SQL 不接受布尔参数；请依据代码和字段提供实际数值或字符状态值"
            )
        if len(parameters) != len(query.parameters):
            raise ReadOnlyQueryError("Oracle 参数名不能仅大小写不同")
        limit = min(query.max_rows, self.max_rows)
        try:
            with closing(self._connect()) as connection:
                connection.autocommit = False
                connection.call_timeout = self.query_timeout_seconds * 1000
                with closing(connection.cursor()) as cursor:
                    cursor.arraysize = limit + 1
                    cursor.prefetchrows = limit + 1
                    cursor.execute("SET TRANSACTION READ ONLY")
                    # 让 Oracle 驱动识别绑定变量，避免把字符串、q 引号或重复参数误当占位符。
                    cursor.prepare(query.sql)
                    if set(cursor.bindnames()) != set(parameters):
                        raise ReadOnlyQueryError("Oracle SQL 绑定变量与 parameters 不一致")
                    cursor.execute(None, parameters)
                    columns = unique_column_names(
                        [str(item[0]) for item in cursor.description or []]
                    )
                    sensitive = sensitive_columns(columns)
                    raw_rows = cursor.fetchmany(limit + 1)
                    rows = []
                    for raw in raw_rows[:limit]:
                        row = {}
                        for column, value in zip(columns, raw, strict=True):
                            if column in sensitive:
                                row[column] = "[REDACTED]"
                            elif isinstance(value, oracledb.LOB):
                                # 必须在连接关闭前处理 LOB；敏感字段不读取，二进制只报告大小。
                                row[column] = (
                                    f"[BINARY {value.size()} bytes]"
                                    if value.type in {oracledb.DB_TYPE_BLOB, oracledb.DB_TYPE_BFILE}
                                    else safe_value(value.read(1, 501))
                                )
                            else:
                                row[column] = safe_value(value)
                        rows.append(row)
            return QueryResult(
                query_name=query.name,
                columns=columns,
                rows=rows,
                returned_rows=len(rows),
                truncated=len(raw_rows) > limit,
                redacted_columns=sensitive,
            )
        except oracledb.Error as exc:
            raise ReadOnlyQueryError(self._safe_error("Oracle 只读查询失败", exc)) from None

    @staticmethod
    def _safe_error(prefix: str, error: Exception) -> str:
        # 驱动错误可能包含 SQL 和业务值，只保留错误码及可操作的固定说明。
        codes = list(dict.fromkeys(re.findall(r"(?:ORA|DPY|DPI)-\d+", str(error))))
        hints = {
            "ORA-01017": "用户名或密码不正确",
            "ORA-12514": "请核对 Service Name",
            "ORA-12505": "请核对 SID",
            "ORA-12170": "连接超时，请检查地址、端口和网络",
            "DPI-1047": "未找到匹配 Python 位数的 Oracle Client，请核对客户端目录",
            "DPY-3010": "服务端版本不支持 Thin，请核对版本并配置 Thick",
            "DPY-3015": "账号密码验证方式不支持 Thin，请联系 DBA 或配置 Thick",
            "DPY-3001": "服务器要求原生网络加密或校验，请配置 Thick，不要降低数据库安全设置",
            "DPY-4024": "数据库调用超时，请缩小查询范围并检查网络",
            "DPI-1067": "数据库调用超时，请缩小查询范围并检查网络",
            "DPY-2019": "当前进程已使用 Thin，请统一模式后重启 ACE",
            "DPY-2053": "当前进程已使用 Thick，请统一模式后重启 ACE",
        }
        hint = next((hints[code] for code in codes if code in hints), "请核对查询、权限及连接配置")
        return f"{prefix} · {', '.join(codes) or 'driver error'} · {hint}"
