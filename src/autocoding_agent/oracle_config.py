"""Oracle 非秘密连接参数；凭据与原有命名数据源共用系统凭据存储。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autocoding_agent.sqlserver_config import SQLServerAuthentication, SQLServerConfigStore


class OracleConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    server: str = Field(min_length=1, description="Oracle 监听地址")
    port: int = Field(default=1521, ge=1, le=65535, description="监听端口")
    database: str = Field(min_length=1, description="Service Name 或 SID 的值")
    identifier_type: Literal["service_name", "sid"] = Field(
        default="service_name",
        description="连接标识类型，不自动猜测",
    )
    username: str = Field(min_length=1, description="只读数据库账号")
    mode: Literal["thin", "thick"] = Field(default="thin", description="Python Oracle 驱动模式")
    client_lib_dir: str = Field(default="", description="Thick 模式的本地 Instant Client 目录")
    connection_timeout_seconds: int = Field(default=10, ge=1, le=60, description="连接超时秒数")

    @field_validator("server", "database", "username")
    @classmethod
    def simple_connection_fields(cls, value: str) -> str:
        if any(char in value for char in "\x00\r\n();=@/\\"):
            raise ValueError("请填写独立连接字段，不要填写 JDBC URL 或连接描述符")
        return value

    @model_validator(mode="after")
    def client_directory(self):
        if self.mode == "thin" and self.client_lib_dir:
            raise ValueError("Thin 模式不使用 Oracle Client 目录")
        return self

    @property
    def authentication(self):
        # 复用现有凭据存储的账号密码校验，不支持 SYSDBA 或操作系统认证。
        return SQLServerAuthentication.SQL_PASSWORD

    @property
    def reference(self) -> str:
        return f"oracle://{self.server}:{self.port}/{self.database}"


class OracleConfigState(BaseModel):
    config: OracleConnectionConfig | None = None
    has_password: bool = False

    @property
    def configured(self) -> bool:
        return self.config is not None and self.has_password


class OracleConfigStore(SQLServerConfigStore):
    """复用原子保存与凭据回退，只替换配置契约；由数据源服务指定命名文件。"""

    config_model = OracleConnectionConfig
    state_model = OracleConfigState
    label = "Oracle"
