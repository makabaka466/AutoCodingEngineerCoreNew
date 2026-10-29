"""按项目与数据源读取有界菜单目录；缓存仅保存导航字段，不保存业务查询结果。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autocoding_agent.adapters.database_safety import validate_read_only_sql
from autocoding_agent.database_models import DataQuery, QueryParameter
from autocoding_agent.ports.runtime import RuntimeInterruptedError

if TYPE_CHECKING:
    from autocoding_agent.datasources import TaskDatabaseAccess

MAX_CATALOG_CHARACTERS = 120_000
_ROW_LIMITS = {"menu_id": 240, "menu_name": 400, "menu_path": 500}


class MenuCatalogConfig(BaseModel):
    """项目维护者提供的菜单投影；必须有唯一 ID，不能把用户关键词加进目录 SQL。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    datasource_id: str = Field(
        pattern=r"^(?:ds-[a-zA-Z0-9_-]+|menu_default)$", description="项目绑定的菜单数据源 ID。"
    )
    sql: str = Field(
        min_length=1,
        max_length=8000,
        description="只读 SELECT，输出 menu_id/menu_name/menu_path；不含排序或分号。",
    )
    parameters: dict[str, QueryParameter] = Field(
        default_factory=dict, description="固定语言、应用等目录范围参数，不包含用户搜索词。"
    )
    page_size: int = Field(
        default=100, ge=1, le=100, description="单页菜单数，不突破只读查询行数上限。"
    )
    max_rows: int = Field(
        default=500, ge=1, le=2000, description="目录总行数上限，超过时明确标为不完整。"
    )
    ttl_seconds: int = Field(
        default=300, ge=0, le=3600, description="完整目录缓存寿命；0 禁用复用。"
    )

    @model_validator(mode="after")
    def validate_projection(self):
        validate_read_only_sql(self.sql)
        if not re.match(r"^SELECT\b", self.sql, re.I) or re.search(
            r"\bORDER\s+BY\b", self.sql, re.I
        ):
            raise ValueError("目录 SQL 应为不含 ORDER BY 的 SELECT 投影，排序分页由宿主完成。")
        if any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or key.casefold().startswith("ace_catalog_")
            for key in self.parameters
        ):
            raise ValueError("目录参数须使用普通名称，不能占用 ace_catalog_ 分页参数。")
        return self


class CatalogSnapshot(BaseModel):
    """complete 仅表示本次分页读取到末尾；菜单可能在读取期间变化，并非事务快照。"""

    model_config = ConfigDict(extra="forbid")
    rows: list[dict[str, str]] = Field(
        default_factory=list,
        max_length=2000,
        description="仅包含 menu_id/menu_name/menu_path 的导航行。",
    )
    complete: bool = Field(description="是否在所有读取限制内到达目录末尾。")
    reason: str = Field(description="读取完整性说明；不包含原始 SQL 错误或连接信息。")
    cache_hit: bool = Field(default=False, description="是否复用仍有效且来源配置未变的完整目录。")
    source_id: str = Field(description="实际读取的项目数据源 ID。")
    fetched_at: datetime = Field(description="本次分页读取开始的 UTC 时间，不表示数据库事务时点。")
    digest: str = Field(description="规范化导航行的 SHA256，标识这份目录内容。")

    @field_validator("rows")
    @classmethod
    def validate_rows(cls, rows):
        if any(
            set(row) != set(_ROW_LIMITS)
            or any(len(row[key]) > limit for key, limit in _ROW_LIMITS.items())
            for row in rows
        ):
            raise ValueError("目录缓存字段或长度无效")
        if len(json.dumps(rows, ensure_ascii=False)) > MAX_CATALOG_CHARACTERS:
            raise ValueError("目录缓存超过文本上限")
        return rows


def _digest(rows: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class MenuCatalogService:
    def __init__(self, data_dir: str | Path) -> None:
        self.cache_dir = Path(data_dir).resolve() / "navigation" / "cache"

    def load(
        self,
        session: Any,
        config: MenuCatalogConfig,
        access: TaskDatabaseAccess,
        *,
        refresh: bool = False,
        stop_requested: Callable[[], bool] | None = None,
    ) -> CatalogSnapshot:
        """同一次调用读完有界分页；没有完整目录时返回明确缺口，不做关键词排名。"""
        if stop_requested and stop_requested():
            raise RuntimeInterruptedError("目录读取已停止，未启动下一页查询。")
        binding = session.database_binding
        if (
            binding is None
            or not session.project
            or binding.project != session.project
            or config.datasource_id not in binding.binding.source_ids
            or config.datasource_id not in binding.sources
            or access.service is None
        ):
            raise ValueError("菜单目录需要明确的项目绑定数据源，不使用旧全局连接。")
        # 命中缓存也要重新核对授权、环境和连接指纹，不能靠旧缓存跨过配置漂移检查。
        if access.service.snapshot(session.project) != binding:
            raise ValueError("菜单数据源授权或配置已变化，请核对后新建任务。")
        source = binding.sources[config.datasource_id]
        if source.database_type not in {"sqlserver", "oracle"}:
            raise ValueError("菜单目录仅支持项目绑定的 SQL Server 或 Oracle 数据源。")
        key = hashlib.sha256(
            json.dumps(
                {
                    "project": session.project,
                    "workspace": str(Path(session.workspace).resolve()),
                    "binding": binding.model_dump(mode="json"),
                    "config": config.model_dump(mode="json"),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        cache_file = self.cache_dir / f"{key}.json"
        now = datetime.now(timezone.utc)
        if refresh:
            cache_file.unlink(missing_ok=True)
        elif config.ttl_seconds and cache_file.is_file():
            try:
                if cache_file.stat().st_size > MAX_CATALOG_CHARACTERS * 6:
                    raise ValueError("缓存过大")
                cached = CatalogSnapshot.model_validate_json(cache_file.read_text(encoding="utf-8"))
                age = (now - cached.fetched_at).total_seconds()
                if (
                    cached.complete
                    and cached.source_id == config.datasource_id
                    and 0 <= age <= config.ttl_seconds
                    and cached.digest == _digest(cached.rows)
                ):
                    return cached.model_copy(update={"cache_hit": True})
            except (OSError, ValueError, TypeError):
                pass  # 缓存损坏或时间无效只触发重读，不能伪装成可用目录。

        rows: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        position = 0
        characters = 2
        complete = False
        reason = "目录尚未完整读取。"
        pages = 0
        while True:
            if pages >= 25:
                reason = "达到 25 页读取上限，目录不完整，请调整配置范围或分页大小。"
                break
            pages += 1
            if stop_requested and stop_requested():
                raise RuntimeInterruptedError("目录读取已停止，未启动下一页查询。")
            # 最后一页额外探测一行，区分恰好达到上限与仍有未读取菜单。
            page_size = min(config.page_size, config.max_rows - position + 1)
            query = DataQuery(
                name=f"ace_menu_catalog_{position}",
                purpose="读取所选项目的菜单目录导航字段。",
                datasource_id=config.datasource_id,
                sql=(
                    "SELECT menu_id, menu_name, menu_path FROM ("
                    "SELECT ace_menu.menu_id, ace_menu.menu_name, ace_menu.menu_path, "
                    "ROW_NUMBER() OVER (ORDER BY ace_menu.menu_id) AS ace_position FROM ("
                    + config.sql
                    + ") ace_menu) ace_page "
                    "WHERE ace_position > :ace_catalog_start AND ace_position <= :ace_catalog_end "
                    "ORDER BY ace_position"
                ),
                parameters={
                    **config.parameters,
                    "ace_catalog_start": position,
                    "ace_catalog_end": position + page_size,
                },
                max_rows=page_size,
            )
            try:
                result = access.execute(session, query, "page_lookup")
            except RuntimeInterruptedError:
                raise
            except Exception:
                raise ValueError(
                    "菜单目录查询失败，请核对项目、数据源、目录 SQL 与字段别名。"
                ) from None
            if (
                result.source is None
                or result.source.datasource_id != config.datasource_id
                or result.source.database_type != source.database_type
            ):
                raise ValueError("目录查询返回的来源与项目绑定不一致。")
            if (
                result.truncated
                or result.returned_rows != len(result.rows)
                or len(result.rows) > page_size
            ):
                reason = "单页结果被截断或行数不一致，目录不完整，请核对查询配置。"
                break
            invalid = False
            for raw in result.rows:
                if position >= config.max_rows:
                    reason = "菜单数量超过配置上限，当前目录不完整；请调整目录范围或上限。"
                    invalid = True
                    break
                values = {key.casefold(): value for key, value in raw.items()}
                if not set(_ROW_LIMITS).issubset(values):
                    reason = "目录缺少 menu_id/menu_name/menu_path 别名，当前目录不完整。"
                    invalid = True
                    break
                row = {
                    key: str(values[key]).strip() if values[key] is not None else ""
                    for key in _ROW_LIMITS
                }
                if (
                    not row["menu_id"]
                    or not row["menu_name"]
                    or any(len(row[key]) > limit for key, limit in _ROW_LIMITS.items())
                    or any(
                        value.endswith("…") or value.startswith("[BINARY ")
                        for value in row.values()
                    )
                ):
                    reason = "目录字段为空、过长或被截断，当前目录不完整。"
                    invalid = True
                    break
                if row["menu_id"] in seen_ids:
                    reason = "菜单 ID 重复，可能配置缺少唯一键或目录读取期间变化；当前目录不完整。"
                    invalid = True
                    break
                size = len(json.dumps(row, ensure_ascii=False)) + 2
                if characters + size > MAX_CATALOG_CHARACTERS:
                    reason = "目录文本达到上下文上限，当前目录不完整；请缩小配置范围。"
                    invalid = True
                    break
                rows.append(row)
                seen_ids.add(row["menu_id"])
                characters += size
                position += 1
            if invalid:
                break
            if len(result.rows) < page_size:
                complete = True
                reason = "已读取本次分页目录至末尾；它是定位线索，不是页面身份或事务一致性证明。"
                if any(not row["menu_path"] for row in rows):
                    reason += "其中无路径条目仅作菜单层级线索。"
                break
        if stop_requested and stop_requested():
            raise RuntimeInterruptedError("目录读取已停止，本次结果未写入缓存。")
        snapshot = CatalogSnapshot(
            rows=rows,
            complete=complete,
            reason=reason,
            source_id=config.datasource_id,
            fetched_at=now,
            digest=_digest(rows),
        )
        if complete and config.ttl_seconds:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_dir / f".{key}.{uuid4().hex}.tmp"
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    handle.write(snapshot.model_dump_json())
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, cache_file)
            finally:
                temporary.unlink(missing_ok=True)
        return snapshot
