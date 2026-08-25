# 任务记录：ACE-INCIDENT-003 异常 Event/Recovery 与自治 SQL 调查

- 任务编号：`ACE-INCIDENT-003`
- 当前状态：`done`
- 下一步责任方：维护方用真实异常样本进行模型与 SQL Server 联调，再规划受控处置状态机
- 创建日期：2026-08-25
- 用户授权范围：重构本项目异常诊断流程；继续使用已配置 SQL Server 的只读连接；完成后按
  版本规则推送 GitHub 一次。

## 1. 目标

1. 异常流程不再依赖直接修改 `session.status` 推进生命周期，而是复用通用 `TaskState`、
   `AgentEvent`、`RuntimeRunRecord`、状态机和恢复扫描内核；
2. 模型从页面相关代码与数据库 schema 中提取/形成最小只读 SQL，由宿主自动执行并把结果返回
   当前模型会话，不能要求用户手工运行 SQL；
3. 查询、状态转换、Runtime 运行和中断恢复形成可回放审计，但不持久化原始业务行或密码；
4. SQL 错误在安全边界内自动返回模型修正，超过次数或缺少连接时才形成明确失败。

## 2. 验收标准

- 新异常任务形成 `CREATED -> INSPECTING -> ...` 的合法状态事件链；
- 页面名称需要数据库映射时，允许先查询 `Menu` 等元数据再定位代码，不强制先伪造页面；
- `query_required` 不展示为让用户执行的步骤，宿主直接调用 `DatabaseReader.execute()`；
- 仅接受参数化、限时、限行、脱敏、只读 SQL；连接凭据永不进入模型上下文；
- 记录 SQL 指纹、参数名、用途、行数和截断信息，不保存 SQL 参数值和原始业务行；
- 进程在异常只读 Runtime 中断后，重启可识别孤儿 run 并进入 `PAUSED`，不自动重放；
- 用户可选择继续只读调查、重新调查或取消；
- 旧 JSON 异常会话可迁移，开发流程回归不受影响；
- 完整非 live 测试、Ruff、diff 检查通过，工程经验文档同步并推送一个版本。

## 3. 当前事实与决策

- 已确认：现有 `IncidentEngine` 实际已经能接收结构化 `DataQuery` 并自动调用数据库，但仍使用
  `JsonIncidentStore` 和直接状态字段，Runtime 没有 run lease/event recovery；
- 已确认：`SQLServerDatabaseReader` 已具备只读校验、参数绑定、超时、限行、敏感列脱敏和
  `ApplicationIntent=ReadOnly`，本次复用，不把凭据交给模型；
- 决策：异常流程保持诊断只读，孤儿 Runtime 统一恢复到 `PAUSED`；它没有仓库/数据库写副作用，
  但仍需用户明确恢复以防重复昂贵查询；
- 决策：共享核心对象与恢复扫描策略，异常快照使用同一 runtime SQLite 文件中的独立表，避免把
  两种不同 Pydantic aggregate 强行转换；
- 决策：页面定位 SQL 也是调查证据，因此 `QUERY_REQUIRED` 可以在尚未形成 `LocatedPage` 时出现。

## 4. 回退方式

本迭代发布前的稳定点为 `v0.4.1` / `04bb4d2`。任何迁移问题都可回退该 tag；旧 JSON 异常文件
只读取和导入，不删除或覆盖。

## 5. 验证记录

| 日期 | 验证 | 结果 | 结论 |
| --- | --- | --- | --- |
| 2026-08-25 | 页面未定位前 Menu 查询 | 模型结构化 SQL 由宿主自动执行 | 已确认 |
| 2026-08-25 | 查询失败自动修正 | 脱敏错误回模型，第二条 SQL 成功 | 已确认 |
| 2026-08-25 | Incident Event Store | sequence 连续、状态可回放、旧 revision 被拒绝 | 已确认 |
| 2026-08-25 | 旧异常 JSON 迁移 | 幂等导入并保留原文件 | 已确认 |
| 2026-08-25 | 孤儿异常 Runtime | 启动后 paused、零次自动模型重放、显式恢复成功 | 已确认 |
| 2026-08-25 | 桌面恢复入口 | 继续只读、重新调查和取消控件可用 | 已确认 |
| 2026-08-25 | Incident CLI | `resume`/`cancel` 帮助和枚举参数正常 | 已确认 |
| 2026-08-25 | 完整非 live 回归 | 120 passed | 已确认 |
| 2026-08-25 | Ruff / `git diff --check` | passed，仅换行提示 | 已确认 |

## 6. 交付物

- `src/autocoding_agent/core/recovery/scanner.py`
- `src/autocoding_agent/adapters/sqlite_incident_store.py`
- `src/autocoding_agent/incident/recovery.py`
- `src/autocoding_agent/incident/engine.py`
- `src/autocoding_agent/incident/application.py`
- `src/autocoding_agent/interfaces/desktop_ui.py`
- `src/autocoding_agent/interfaces/incident_cli.py`
- `tests/test_incident_flow.py`
- `tests/test_sqlite_incident_store.py`
- `tests/test_incident_recovery.py`
- `docs/PROJECT_EXPERIENCE.md`

## 7. 剩余边界

- 尚未使用用户真实 SQL Server 和真实异常样本做 live 联调；本迭代不主动访问生产数据；
- 当前异常流程仍只读诊断，不实施代码修复、不写数据库；
- 两个 SQLite store 有部分可进一步抽取的事务代码，但当前领域模型保持清晰；
- SQL 安全仍是保守文本校验加只读连接，生产必须继续使用只有 SELECT 权限的账号。
