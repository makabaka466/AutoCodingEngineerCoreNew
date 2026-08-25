# 任务记录：ACE-DATA-005 数据库查询超时与限行

- 任务编号：`ACE-DATA-005`
- 当前状态：`done`
- 下一步责任方：用户在真实 SQL Server 环境观察 60 秒超时与 100 条首轮采样效果
- 创建日期：2026-08-25
- 目标：为开发与异常处理流程的自动数据库查询统一设置 60 秒执行超时，并在结果数量
  不明确时默认先查询 100 条，降低长查询和大结果集对数据库、模型上下文及本地日志的影响。

## 验收标准

- SQL Server 与 SQLite Reader 的默认单条查询超时均为 60 秒；
- 开发与异常流程共用的 `DataQuery` 默认行数为 100，宿主最多向模型返回 100 条；
- 模型规则明确要求数量未知时先做 100 条有界采样，并在足够时使用更小限制；
- 显式传入小于 100 的行数仍被保留，宿主配置仍可进一步收紧；
- 示例配置、README、接口文档与项目工程经验同步更新；
- 相关聚焦测试、完整非 live 回归、Ruff 和 diff 检查通过；
- 形成 `v0.5.2` 单一功能提交、annotated tag，并向 `origin` 推送一次。

## 当前状态与关键发现

- 两套流程都使用 `DataQuery` 和 `DatabaseReader`，可通过共享契约统一默认行为；
- SQL Server 查询超时必须设置在 pyodbc connection 上，cursor 不支持 `timeout` 属性；
- `fetchmany(limit + 1)` 已能强制控制进入模型上下文的结果量，并判断是否截断；
- 仅限制客户端取数不足以表达 Agent 的查询意图，还需提示模型在 SQL 中使用适合方言的
  `TOP`/`LIMIT`，避免无界查询成为默认策略。

## 决策记录

- 共享默认值放在 `database_models.py`，配置、SQL Server 与 SQLite 适配器共同引用，避免漂移；
- `DataQuery.max_rows` 的结构化上限保持 100；宿主配置可以收紧，不能放大模型返回上限；
- SQL Server 登录超时仍由连接配置控制，本任务只把 SQL 执行超时默认值改为 60 秒；
- schema 元数据查询与业务行查询分开处理；本任务的 100 条限制针对进入模型的业务查询结果。

## 验证记录

| 日期 | 验证 | 结果 | 结论 |
| --- | --- | --- | --- |
| 2026-08-25 | 代码路径与现有防护检查 | 已完成 | 已确认 |
| 2026-08-25 | `pytest tests/test_sqlserver_database.py tests/test_database_defaults.py -q` | 14 passed | 已确认 |
| 2026-08-25 | `pytest -m "not live" -q` | 124 passed | 已确认 |
| 2026-08-25 | `ruff check src tests` | passed | 已确认 |
| 2026-08-25 | `git diff --check` | passed，仅换行提示 | 已确认 |

## 影响面与回退

- 影响：共享数据库查询契约、SQL Server/SQLite Reader 默认值、两套流程提示词、配置与文档；
- 不影响：连接凭据、SQL 只读校验、参数绑定、查询轮次、事件审计和原始业务行不落盘规则；
- 回退：发布前可丢弃本任务文件变更；发布后可切回不可变 tag `v0.5.1`。

## 交付物

- `src/autocoding_agent/database_models.py`
- `src/autocoding_agent/config.py`
- `src/autocoding_agent/adapters/sqlserver_database.py`
- `src/autocoding_agent/adapters/sqlite_database.py`
- `src/autocoding_agent/skills/registry.py`
- `src/autocoding_agent/incident/engine.py`
- `tests/test_sqlserver_database.py`
- `tests/test_database_defaults.py`
- `README.md`
- `docs/INTERFACES.md`
- `docs/PROJECT_EXPERIENCE.md`

## 发布记录

- 版本：`v0.5.2`
- 发布策略：单一功能提交、不可变 annotated tag、向 `origin` 推送一次
- 上一个稳定回退点：`v0.5.1`
