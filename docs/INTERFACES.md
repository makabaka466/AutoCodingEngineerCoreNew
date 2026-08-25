# AutoCoding Engineer 接口与数据契约

本文记录当前 `0.5.3` 已实现的软件开发、异常诊断、Python、CLI、桌面客户端、Streamlit、
Runtime、持久化和状态契约。
设计动机和运行流程见[架构说明](ARCHITECTURE.md)。

## 1. 公共 Python API

包根 `autocoding_agent` 公开以下名称：

```python
from autocoding_agent import (
    AgentApplication,
    AgentOutcome,
    AgentStatus,
    build_application,
)
```

### 1.1 `build_application`

```python
def build_application(
    settings: Settings | None = None,
    runtime: AgentRuntime | None = None,
    database: DatabaseReader | None = None,
    database_reference: str | None = None,
) -> AgentApplication
```

- 未传 `settings` 时使用进程缓存的 `get_settings()`。
- 未传 `runtime` 时创建 `ClaudeCodeRuntime`。
- 创建数据目录，并使用 SQLite Task/Event Store、文件型 Artifact/能力存储与 Git workspace observer。
- 启动时运行 RecoveryManager，结果通过 `AgentApplication.recovery_scan` 暴露。
- 配置本地滚动日志，并通过 `AgentApplication.log_path` 暴露日志文件路径。
- `runtime` 参数可用于测试或替换模型执行适配器。
- `database` 与无凭据 `database_reference` 可把同一只读数据源注入开发流程；模型仅能在
  `inspect` 返回查询计划，不能直接获得数据库工具。

### 1.2 `AgentApplication`

`AgentApplication` 是 CLI、UI 和其他调用方应使用的稳定门面。

| 方法 | 输入 | 行为和返回 |
| --- | --- | --- |
| `start(workspace, message, project=None)` | `str | Path`, `str`, `str | None` | 新建任务，保存所选知识项目并执行首个只读轮次，返回 `AgentOutcome` |
| `send(session_id, message, command_id=None)` | `str`, `str`, `str | None` | 补充澄清、修订指令，或从 completed 开启同一会话的新工作轮次；可用命令 ID 幂等重试 |
| `approve(session_id, command_id=None)` | `str`, `str | None` | 批准当前请求的精确 scope，并以对应模式继续 |
| `reject(session_id, reason="", command_id=None)` | `str`, `str`, `str | None` | 拒绝当前请求，以只读模式继续并要求替代方案 |
| `resume(session_id, action="read_only_inspect")` | `str`, `RecoveryAction | str` | 从 paused/recovery_required 以显式安全动作恢复 |
| `pause(session_id)` | `str` | 在持久化边界暂停非终态任务 |
| `cancel(session_id)` | `str` | 取消任务且不重放 Runtime |
| `outcome(session_id)` | `str` | 返回最近一次持久化结果 |
| `get_session(session_id)` | `str` | 返回完整 `AgentSession`，包含消息和事件 |
| `list_sessions()` | 无 | 按 `updated_at` 倒序返回所有会话 |
| `events(session_id)` | `str` | 返回持久化事件时间线 |
| `artifacts(session_id)` | `str` | 返回 Artifact 元数据，不直接输出敏感正文 |
| `runs(session_id)` | `str` | 返回 Runtime run 生命周期记录 |
| `explain_change(session_id, path)` | `str`, `str` | 聚合与相对路径相关的 Decision、Artifact 和 Event |

主要前置条件：

- `start` 的工作区必须真实存在且为目录，任务消息不能为空；
- `project` 对应当前流程 `knowledge/` 下的二级路径；桌面端要求新任务必须选择；
- `send` 的消息不能为空；`completed` 会开启下一工作轮次，`cancelled` 不能重新打开；
- `approve`、`reject` 只适用于存在 `pending_approval` 的会话；
- session ID 必须是已保存的 UUID。

这些方法当前是同步、阻塞调用。它们可能抛出 `ValueError`、`KeyError`、路径解析异常或存储
异常；模型执行期间的多数 Runtime/契约/策略异常会由 Engine 转换成 `failed` Outcome。

## 2. 核心枚举

### 2.1 `TaskState`

`TaskState` 是开发任务的持久化生命周期，不等同于模型决定或工具权限：

| 值 | 含义 |
| --- | --- |
| `created` | Task snapshot 已创建，尚未启动 Runtime |
| `inspecting` | 正在进行只读理解、调查或方案设计 |
| `waiting_input` | 等待用户补充一个关键信息 |
| `querying_data` | 宿主正在执行模型提出的受限只读查询 |
| `waiting_modify_approval` | 等待用户批准结构化修改方案 |
| `implementing` | 已授权 Edit/Write 的实施轮次 |
| `waiting_verify_approval` | 等待用户批准验证命令 |
| `verifying` | 已授权白名单 Bash 的验证轮次 |
| `replanning` | 验证失败后的有上限重新规划 |
| `paused` | 用户暂停或孤儿只读运行的安全停靠状态 |
| `recovery_required` | 中断后副作用不确定，需要恢复决策 |
| `completed` | 当前工作轮次已完成；新用户消息可转回 inspecting |
| `failed` | 明确失败；兼容路径可允许 send 后重新调查 |
| `cancelled` | 用户取消终态 |

只有 `AgentStateMachine.transition()` 可以修改 `AgentSession.task_state`。每次真实转换会增加
`version` 并追加 `state_transitioned`；重复转换到相同状态是幂等 no-op。持久化 command receipt
保证相同命令 ID 不会重复调用 Runtime。

### 2.2 `AgentStatus`

| 值 | 含义 |
| --- | --- |
| `needs_input` | 信息不足，需要用户再提供一条消息 |
| `query_required` | 开发调查需要主机执行一组受限只读查询；内部循环通常不会直接返回给 UI |
| `approval_required` | 需要用户批准修改或验证范围 |
| `completed` | 当前任务已如实到达终态 |
| `failed` | Runtime、输出契约或策略检查失败 |

`AgentStatus` 是模型本轮结构化决定/公开 Outcome 类型，不是任务生命周期。`completed` 会触发
本轮独立能力文档保存；新的 `send` 会增加 cycle 并重新进入只读轮次。`failed` 当前也可以通过
`send` 重新进入只读轮次。

### 2.3 `AgentMode`

| 值 | 用途 |
| --- | --- |
| `inspect` | 澄清、搜索、阅读和诊断 |
| `implement` | 用户批准后编辑或写入工作区文件 |
| `verify` | 用户批准后执行白名单内的验证命令 |

### 2.4 其他枚举

- `ApprovalScope`：`modify`、`verify`。
- `MessageRole`：`user`、`assistant`、`system`；当前 Engine 追加的是 user/assistant。
- `AgentCommandType`：`create_task`、`submit_user_input`、`grant_approval`、
  `reject_approval`、`resume_task`、`pause_task`、`cancel_task`；
- `FailureClass`：Runtime、Provider、Policy、Validation、副作用不确定和终态失败分类；
- `EventType`：除任务、状态、输入、审批、完成、失败、能力和数据库事件外，还包括
  `runtime_started/activity/completed/failed/interrupted`、`tool_started/finished`、
  `code_modified`、`test_executed`、`verification_failed`、`recovery_required`、
  `decision_recorded`、`artifact_recorded/failed`、`task_reopened`。
- `RecoveryAction`：`read_only_inspect`、`replan`、`cancel`。

## 3. 结构化模型契约

所有模型均为 Pydantic `BaseModel`。

### 3.1 消息、证据和审批

```text
ChatMessage
  role: MessageRole
  content: str
  created_at: datetime (UTC 自动生成)
```

```text
Evidence
  path: str | None = None
  summary: str
```

`Evidence.path` 如果存在，必须是工作区相对路径，不能是绝对路径或包含 `..`。当前主机不
校验文件是否存在。

```text
ProposedChange
  path: str | None = None
  area: non-empty str
  current: non-empty str
  proposed: non-empty str

ChangeProposal
  summary: non-empty str
  changes: list[ProposedChange] (至少一项)
  expected_result: non-empty str
  impact: list[non-empty str] = []
  validation: list[non-empty str] = []
  preview_markdown: non-empty str | None = None

ApprovalRequest
  scope: ApprovalScope
  reason: str
  proposed_actions: list[str] = []
  proposal: ChangeProposal | None
```

`proposal` 键在 Runtime JSON Schema 中必须存在。`modify` 必须传完整对象，`verify` 传
`null`。`preview_markdown` 由模型在适合时提供界面线框、API/数据示例、伪代码或行为前后
对比；不适合时保持 `null`，UI 会明确说明无法可靠预览。存储加载器兼容旧 JSON 中完全缺少
`proposal` 键的会话，但旧修改审批不能直接执行。

### 3.2 能力草稿

```text
CapabilityDraft
  title: str
  summary: str
  triggers: list[str] = []
  method: list[str] = []
  validation: list[str] = []
  risks: list[str] = []
```

它由模型在完成决定中提供，是能力 Markdown 的内容来源；缺失时存储器会生成 fallback。

### 3.3 `AgentDecision`

```text
AgentDecision
  status: AgentStatus
  message: str
  reason: str | None = None
  alternatives: list[str] = []
  confidence: float | None = None (0–1)
  risk_level: RiskLevel | None = None
  evidence: list[Evidence] = []
  next_actions: list[str] = []
  approval: ApprovalRequest | None = None
  changed_files: list[str] = []
  test_summary: str | None = None
  capability: CapabilityDraft | None = None
  queries: list[DataQuery] = []
```

这是 Claude Code 可以返回的唯一业务决定格式，也是传给 `--json-schema` 的 Schema 来源。

契约约束：

- `status=approval_required` 时必须存在 `approval`；
- 其他状态不允许携带 `approval`；
- `modify` 审批必须包含 `ChangeProposal`，否则 Engine 把该轮标记为失败；
- `inspect` 模式不允许非空 `changed_files`；
- `query_required` 必须包含 1–5 条查询，其他状态不得携带查询；查询只允许出现在 `inspect`；
- 所有 `changed_files`、非空 evidence path 和 proposal path 必须是安全的相对路径。

`message` 是用户可见的 Markdown 文本。`next_actions`、`evidence`、`changed_files` 和
`reason/alternatives/confidence/risk_level` 用于生成 DecisionRecord；`test_summary` 只是模型
声明，默认不具备 host_verified 资格。桌面客户端会额外展示 evidence、changed files、测试摘要和
能力文档路径。桌面与 Web UI 都会完整展示结构化修改方案和预览，CLI 输出其关键字段。

### 3.4 使用量与事件

```text
AgentUsage
  input_tokens: int = 0
  output_tokens: int = 0
  cache_read_tokens: int = 0
  cost_usd: float | None = None
  duration_ms: int | None = None
  turns: int | None = None
```

```text
AgentEvent
  id: str (UUID 自动生成)
  sequence: int | None（Event Store 分配）
  schema_version: int = 1
  type: EventType
  message: str
  reason: str | None
  alternatives: list[str] = []
  confidence: float | None
  risk_level: RiskLevel | None
  actor: str = "host"
  correlation_id: str | None
  causation_id: str | None
  command_id: str | None
  data: dict[str, Any] = {}
  created_at: datetime (UTC 自动生成)
```

事件同时保存在 Task snapshot 和独立 Event 表；SQLiteTaskStore 为每个 task 分配连续 sequence，
Event ID 全局唯一，已追加事件不能被修改。一次新建并成功执行的 Runtime 轮次通常产生：

```text
task_created
state_transitioned (created -> inspecting)
turn_started
runtime_started
runtime_activity / tool_started / tool_finished (0..n)
runtime_completed | runtime_failed | runtime_interrupted
decision_recorded
artifact_recorded (0..n)
state_transitioned (inspecting -> waiting/completed/failed/recovery_required)
input_required | approval_required | task_completed | task_failed
[capability_saved | capability_failed，仅 completed]
```

Runtime 或策略异常会留下 run 终态；实施/验证副作用不确定时使用 `recovery_required`，不会把
缺少最终 result 误写成普通 `task_failed` 后自动重试。

`AgentCommand` 由 AgentEngine 的 start/send/approve/reject/resume/pause/cancel 入口创建：

```text
AgentCommand
  id: str
  task_id: str
  type: AgentCommandType
  expected_version: int
  actor: str = "user"
  payload: dict = {}
  created_at: datetime
```

命令 ID 会进入相关事件。task snapshot 使用 revision compare-and-swap 拒绝跨进程旧快照；
`send/approve/reject` 已开放 `command_id`，成功终结后写入不可变 CommandReceipt。同一 ID 重试
返回当前持久化 Outcome，不再次执行 Runtime；若只有起始事件而没有 receipt，则拒绝不安全重放。

### 3.5 `AgentSession`

```text
AgentSession
  id: str (UUID 自动生成)
  workspace: str
  goal: str
  project: str | None = None
  task_state: TaskState = created
  version: int = 0
  revision: int = 0
  runtime_session_id: str | None = None
  status: AgentStatus | None = None
  pending_approval: ApprovalRequest | None = None
  last_decision: AgentDecision | None = None
  last_usage: AgentUsage = empty usage
  capability_document: str | None = None
  database_reference: str | None = None
  query_observations: list[QueryObservation] = []
  query_rounds: int = 0
  replan_rounds: int = 0
  cycle_number: int = 1
  cycle_objective: str | None = None
  cycle_query_observation_start: int = 0
  messages: list[ChatMessage] = []
  events: list[AgentEvent] = []
  decision_records: list[DecisionRecord] = []
  artifacts: list[ArtifactRecord] = []
  runs: list[RuntimeRunRecord] = []
  command_receipts: list[CommandReceipt] = []
  created_at: datetime
  updated_at: datetime
```

`id` 是应用会话 ID；`runtime_session_id` 是 Claude Code 返回、用于下一轮 `--resume` 的 ID。
两者不能假定始终相同。`workspace` 以严格解析后的绝对目录保存。
`version` 只在生命周期转换时增加；`revision` 在每次 Task snapshot 保存时增加。旧 JSON 没有
`task_state/version/revision` 时，会根据已有 status 和 pending approval 推导生命周期，以 0
载入后导入 SQLite；旧文件不会被覆盖。旧会话没有 cycle 字段时默认属于第 1 轮。
`cycle_query_observation_start` 只划分当前轮次的公开结果和能力文档，历史查询审计仍保留在 Session
和 Event 中。

### 3.6 Decision、Artifact 与 Runtime Run

```text
DecisionRecord
  id, task_id, event_id
  decision_type, summary, reason
  evidence: list[EvidenceRef]
  alternatives, confidence, risk_level
  actor, model, runtime_session_id, created_at

ArtifactRecord
  id, task_id, event_id, type
  relative_path, sha256, size_bytes, schema_version
  source, host_verified, sensitive
  related_paths, metadata, created_at

RuntimeRunRecord
  id, task_id, state, mode
  owner_id, owner_pid
  status: started | completed | failed | interrupted
  started_at, heartbeat_at, completed_at, terminal_reason
  runtime_session_id, activity_ids
```

DecisionRecord 表达“为什么这样判断”；Artifact/Runtime/Event 表达宿主观察到的内容。三者都带来源
ID，但只有 `host_verified=true` 的 Artifact 或明确宿主事件可以作为执行事实。

### 3.7 `AgentOutcome`

```text
AgentOutcome
  session_id: str
  workspace: str
  status: AgentStatus
  task_state: TaskState
  cycle_number: int
  message: str
  evidence: list[Evidence] = []
  next_actions: list[str] = []
  approval: ApprovalRequest | None = None
  changed_files: list[str] = []
  test_summary: str | None = None
  capability_document: str | None = None
  query_observations: list[QueryObservation] = []
  usage: AgentUsage
  events: list[AgentEvent] = []
```

这是每次应用操作的公开返回值。`events` 当前包含该会话至今的完整事件列表，而不是仅本轮
增量；`query_observations` 只返回当前 cycle 的查询摘要。`capability_document` 是当前完成轮次
文档在状态目录中的绝对路径，不是目标仓库内路径。

## 4. Runtime 端口与 Claude Code 契约

### 4.1 `AgentRuntime`

```python
class AgentRuntime(Protocol):
    def run(self, turn: RuntimeTurn) -> RuntimeResult: ...
```

自定义 Runtime 必须接收完整 `RuntimeTurn` 并返回经契约表达的 `RuntimeResult`。
开发 Engine 会在适配器提供时优先调用：

```python
def run_observed(
    turn: RuntimeTurn,
    *,
    run_id: str,
    event_sink: Callable[[RuntimeActivity], None],
) -> RuntimeResult: ...

def interrupt(run_id: str) -> bool: ...
```

`event_sink` 在 Runtime 运行中增量持久化脱敏活动；`interrupt` 只控制当前进程生命周期，不表示
已执行工具的副作用被撤销。仅实现 `run()` 的旧适配器仍可使用，但没有细粒度活动事件。

```text
RuntimeTurn
  session_id: str
  runtime_session_id: str | None
  workspace: str
  user_message: str
  history: list[ChatMessage] = []
  mode: AgentMode
  system_prompt: str
  tools: list[str]
  allowed_tools: list[str]
  permission_mode: str = "dontAsk"
  capability_dir: str | None = None
```

```text
RuntimeResult
  decision: AgentDecision
  runtime_session_id: str
  usage: AgentUsage = empty usage
```

Engine 期望 `run()` 完成一轮并阻塞到有结果。自定义 Runtime 应遵守 `tools`、
`allowed_tools` 和 `permission_mode`；Engine 仍会执行模式与路径二次校验。

异常流程使用通用结构化端口：

```python
class StructuredRuntime(Protocol):
    def run_structured(
        self,
        turn: RuntimeTurn,
        response_model: type[StructuredOutputT],
    ) -> StructuredRuntimeResult[StructuredOutputT]: ...
```

`ClaudeCodeRuntime.run()` 是对 `run_structured(turn, AgentDecision)` 的兼容封装；异常引擎传入
`IncidentDecision`，因此两个领域共享 Claude Code 进程协议和错误处理，但不共享业务状态。

### 4.2 Claude Code 命令

默认适配器构造的命令等价于：

```text
claude -p
  --bare
  --no-chrome
  --strict-mcp-config
  --mcp-config '{"mcpServers":{}}'
  --setting-sources ''
  --output-format stream-json
  --include-hook-events
  --model <configured-model>
  --permission-mode <permission-mode>
  --tools <comma-separated-tools>
  --allowedTools <one or more allowed tool patterns>
  --append-system-prompt <bundled skills and boundaries>
  --json-schema <AgentDecision JSON Schema>
  [--add-dir <workspace capability directory，仅 inspect>]
  [--max-budget-usd <amount>]
  (--session-id <application session id> | --resume <runtime session id>)
  <user message>
```

子进程工作目录固定为 `RuntimeTurn.workspace`。开发可观测路径要求 stdout 是逐行 stream-json，
最终 `result` 行包含以下 JSON envelope；通用 `run_structured()` 兼容路径仍可直接读取最终 JSON：

Windows 运行时还会传入隐藏 `STARTUPINFO` 和 `CREATE_NO_WINDOW`，避免每轮 Claude Code 调用
弹出控制台。Runtime 日志只记录调用元数据、usage 和脱敏错误，不记录完整 prompt。

```json
{
  "session_id": "runtime-session-id",
  "structured_output": {
    "status": "completed",
    "message": "..."
  },
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_input_tokens": 0
  },
  "total_cost_usd": null,
  "duration_ms": 0,
  "num_turns": 0
}
```

`structured_output` 和非空字符串 `session_id` 是必需项。usage、费用和耗时字段可缺省。

适配器可能产生 `ClaudeCodeError`：可执行文件不存在、轮次超时、非零退出、无效 JSON、
缺少结构化结果、决定 Schema 不合法或缺少可恢复会话 ID。常见凭证形式会在子进程错误
文本中被替换为 `[REDACTED]`。

## 5. 会话存储端口

```python
class SessionStore(Protocol):
    def create(self, session: AgentSession) -> None: ...
    def load(self, session_id: str) -> AgentSession: ...
    def save(self, session: AgentSession) -> None: ...
    def list(self) -> list[AgentSession]: ...
```

默认 `SQLiteTaskStore` 把开发任务保存在：

```text
<data_dir>/runtime/agent-runtime.db
```

- `create` 在 UUID 已存在时抛出 `FileExistsError`；
- `load` 在 UUID 格式非法时抛出 `ValueError`，不存在时抛出 `KeyError`；
- `save` 使用 snapshot revision 做乐观并发检查，旧快照抛出 `ConcurrentSessionUpdate`；
- snapshot 更新和新 Event、Decision、Artifact metadata、Run、CommandReceipt 在同一 SQLite 事务；
- `list_events(task_id)` 按 task sequence 返回不可变事件；
- `list_artifacts(task_id)`、`list_decisions(task_id)`、`list_runs(task_id)` 返回审计元数据；
- `replay_task_state(task_id)` 根据状态事件重建生命周期，链断裂抛出 `EventStoreCorruption`；
- `list` 按更新时间倒序返回 snapshot。

数据库表为 `tasks`、`events`、`decisions`、`artifacts`、`runs`、`commands`。Event/Decision/Artifact/
CommandReceipt 插入后不可修改；Run 只允许从 started 转到一个终态，终态记录不可重写。

Artifact 正文由 `TaskArtifactStore` 保存到 `<data_dir>/tasks/<task-id>/artifacts/`。文件名使用 UUID，
manifest 保存类型、哈希、来源和关联路径；正文经过凭据脱敏、大小限制和原子替换。工作区 observer
采集 Git status、commit 与 staged/unstaged diff，不读取未跟踪文件正文。

旧 `<data_dir>/sessions/*.json` 会在 SQLiteTaskStore 初始化时幂等导入；旧
`<data_dir>/incidents/*.json` 会由 `SQLiteIncidentStore` 幂等导入。两者都会补充可回放的
migration 事件且不覆盖旧文件。`JsonSessionStore`/`JsonIncidentStore` 仅保留用于兼容和测试。
异常表为 `incident_tasks`、`incident_events`、`incident_runs`、`incident_commands`，与开发表位于
同一个 runtime 数据库并使用相同 sequence/revision/终态不可变约束。

## 6. 能力存储接口与文件格式

开发 `CapabilityStore` 与异常 `IncidentCapabilityStore` 当前由各自 Engine 直接调用，没有单独
Protocol。两者根目录分别是
`<data_dir>/workspaces/<workspace-id>/development/` 与 `incident/`。
用户基础知识源文件位于项目根目录的
`knowledge/<development|incident>/<二级路径>/<二级路径名>.md`，每个二级路径唯一对应一份
同名文档。`prepare()` 会把当前流程的项目知识同步至工作区 `pinned/` 只读视图并写入本领域
索引，保证开发与异常内容互不混用。`MarkdownKnowledgeService` 提供二级路径列表、创建、读取
和原子保存。

```python
prepare(workspace: str | Path) -> Path
record(
    session: AgentSession,
    decision: AgentDecision,
    model: str,
) -> CapabilityReceipt
```

```text
CapabilityReceipt
  document_path: str
  index_path: str
  created: bool
```

### 6.1 task JSON

第 1 轮使用 `tasks/<session-id>.json`，后续使用
`tasks/<session-id>-cycle-<NNN>.json`。共享字段和开发专有字段包括：

```text
schema_version
task_id
session_id
cycle_number
workspace_id
goal
cycle_objective
outcome
changed_files
test_summary
document
model
completed_at
```

异常记录使用相同的 `session_id/cycle_number/cycle_objective/document/model/completed_at`，并以
`problem` 代替开发记录的 `goal`；开发记录另外保存 changed files 和 test summary。

`document` 是相对于当前工作区能力目录的 POSIX 风格路径。task JSON 的存在也是当前
幂等判断依据。

### 6.2 能力 Markdown

文档 frontmatter 包含 `schema_version`、`session_id`、`cycle_number`、`model`、`completed_at`，
正文包含：

- 标题和摘要；
- 适用场景；
- 方法；
- 验证；
- 风险与边界；
- 任务证据；
- 来源目标、结果和变更文件。

第 1 轮文件名为 `<session-id>.md`，后续为 `<session-id>-cycle-002.md` 等独立文件。同一轮
重复记录返回已有文件，新一轮不会修改或追加旧 MD。文件名不使用模型生成的标题，从根本上
避免标题里的敏感值通过文件名或索引链接泄漏。

`CAPABILITIES.md` 由全部 task JSON 重建，每项链接到对应能力文档，并附最终结果的前
240 个字符。该索引明确提示历史知识可能过期。

## 7. CLI 接口

安装项目后，入口为：

```powershell
autocoding-agent --help
```

| 命令 | 参数 | 作用 |
| --- | --- | --- |
| `start MESSAGE --workspace PATH` | `MESSAGE` 必填；`-w/--workspace` 必填 | 创建并运行新任务 |
| `send MESSAGE --session-id UUID [--command-id ID]` | message/session 必填 | 幂等继续现有任务 |
| `approve --session-id UUID [--command-id ID]` | session 必填 | 幂等批准待处理权限 |
| `reject --session-id UUID [--reason TEXT] [--command-id ID]` | session 必填 | 拒绝权限并继续 |
| `show --session-id UUID` | `-s/--session-id` 必填 | 显示最后结果 |
| `resume --session-id UUID [--action ACTION]` | action 为 read_only_inspect/replan/cancel | 显式恢复暂停或不确定任务 |
| `pause --session-id UUID` | session 必填 | 在持久化边界暂停 |
| `cancel --session-id UUID` | session 必填 | 取消且不重放 Runtime |
| `events --session-id UUID` | session 必填 | 输出事件时间线 JSON |
| `artifacts --session-id UUID` | session 必填 | 输出 Artifact 元数据 JSON |
| `runs --session-id UUID` | session 必填 | 输出 Runtime run JSON |
| `explain PATH --session-id UUID` | path 为工作区相对路径 | 输出修改原因、产物和事件关联 |
| `sessions` | 无 | 列出最近会话 |

示例：

```powershell
autocoding-agent start "调查上传失败原因" --workspace D:\repo
autocoding-agent send "入口是 src/upload.py" --session-id <uuid>
autocoding-agent approve --session-id <uuid>
autocoding-agent events --session-id <uuid>
autocoding-agent explain src/upload.py --session-id <uuid>
autocoding-agent resume --session-id <uuid> --action read_only_inspect
autocoding-agent reject --session-id <uuid> --reason "先不要修改"
autocoding-agent show --session-id <uuid>
autocoding-agent sessions
```

`start/send/approve/reject/show/resume/pause/cancel` 的标准输出格式为：

```text
session: <uuid>
status: <status>
<model message>
[approval: <scope> — <reason>]
[proposal: <summary>]
[- <path-or-area>: <current> -> <proposed>]
[expected: <expected-result>]
[preview: <preview-markdown>]
[capability: <absolute document path>]
```

`events/artifacts/runs/explain/sessions` 输出 JSON；session 列表每项包含 `session_id`、`status`、
`task_state`、`workspace`、`goal`、
`updated_at`。接口操作抛错时 CLI 向 stderr 输出 `Error: ...` 并以退出码 1 结束。

## 8. 原生桌面客户端

安装项目后，桌面入口为：

```powershell
autocoding-agent-client
```

也可以运行 `python -m autocoding_agent.interfaces.desktop_ui`。根目录 `start.cmd` 默认通过
`pythonw.exe` 启动该入口，因此不会启动本地 HTTP 服务或浏览器。

客户端直接调用 `AgentApplication` 与 `IncidentApplication`，提供：

- 启动前自动检测 Claude Code 和模型配置；未就绪时先显示配置页；
- 顶部胶囊式“开发 / 异常处理”选择器，蓝底表示当前流程；
- 对话输入区的“项目”选择框，只列出当前流程的二级路径并显示所用 MD 相对路径；
- 两套流程各自的当前 session、最近会话、欢迎提示、状态和结果渲染；
- 白银浅色玻璃主题，左侧最近会话、中部对话/上下文，以及宽屏右侧真实任务概览；
- 概览按当前流程 Session 计算今日任务、完成、进行中、完成率和七日趋势，并显示本机模型、
  项目知识及 SQL Server 配置状态；宽度低于 1180 px 时自动隐藏；
- 异常模式下只额外显示页面线索；SQL Server 不重复占用输入区，统一从“系统配置”管理；
- 统一系统配置和本地滚动日志目录快捷入口；
- 新任务、持久化聊天记录及同一 session 的多轮补充；
- `approval_required` 的完整修改方案、当前/目标状态、影响、验证计划、预览，以及批准或调整；
- `recovery_required` 的只读检查、重新规划和取消恢复卡，并显示当前 TaskState；
- evidence、changed files、测试摘要和能力文档路径；
- `needs_input`、`approval_required`、`completed`、`failed` 状态提示。
- 已完成会话保持输入框可用，按钮显示“继续对话”；发送后开启下一 cycle。

应用方法是同步接口，因此客户端用一个后台工作线程执行每一轮，并通过 Tk 的事件队列回到
主线程渲染。任务建立后会锁定所选项目，忙碌期间会禁用发送、会话切换和重复审批。Runtime
具备内部进程级 interrupt；界面只在安全持久化边界展示 pause/cancel/recovery 操作，不声称回滚
已经发生的副作用。Windows 下还会使用当前登录
会话内的命名互斥量保持单实例，避免两个桌面窗口并发写同一会话存储。

系统配置是一个窗口、三个页签。模型页字段为 Claude Code 路径、API 地址、模型名称和 API
Key。Claude 路径会通过
`--version` 验证；Key 控件始终为空并以密码形式输入，`has_api_key=true` 时留空保存表示保留
原密钥。保存成功后当前进程立即生效，并重建两套 Runtime，但不删除已有会话。

SQL Server 页包含服务器、端口、数据库、已安装 ODBC 驱动、Windows/SQL Server 认证、
用户名、密码、加密和信任证书选项，并提供后台“测试连接”。非密钥字段保存在
`<data_dir>/database/sqlserver.json`；密码通过 Windows Credential Manager 保存，不进入 JSON、
日志、模型提示词或会话。两套流程共享连接；连接可随时更换，已有开发或异常会话保持原连接，
新配置从对应流程的下一项任务开始。

“MD 能力配置”页不显示项目路径，直接按“开发/异常处理 → 二级路径”导航。点击添加时在项目
`knowledge/` 下创建 `<二级路径>/<二级路径名>.md`，选择路径即可直接编辑并保存；只读路径框
显示项目相对路径。未保存内容在切换流程、路径或关闭窗口前会得到确认。路径名不能包含路径分隔符、
`..`、Windows 保留字符或设备名。

## 9. Streamlit Web UI

UI 是 `AgentApplication` 的薄适配层，控制台入口为：

```powershell
autocoding-agent-ui
```

如果未安装 `ui` 可选依赖，需要先安装 Streamlit。当前页面提供：

- 目标项目路径输入；
- 新建任务；
- 基于持久化 session messages 的聊天记录；
- 审批操作、拟执行动作展示和可选拒绝原因；
- 完成状态和能力文档路径提示。

首次消息调用 `start`，已有会话中的消息调用 `send`。完成后页面继续接收追问或补充要求，
也允许用户点击“新建任务”另开会话。UI 当前没有独立展示完整 evidence、usage、event timeline，也没有
流式 token 或后台任务接口。

`start.cmd -Web` 或 `start.ps1 -Web` 才会启动该页面；`-Port` 与 `-NoBrowser` 仅作用于
Web 模式。

## 10. 环境配置

`Settings` 使用 `.env` 和环境变量，前缀为 `AUTO_CODING_`。
桌面入口默认通过 `ClaudeModelSetupService` 管理 Claude 路径和模型服务变量；`.env` 主要用于
无界面部署和高级覆盖。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTO_CODING_CLAUDE_COMMAND` | 自动发现，最终回退 `claude` | Claude Code 可执行命令或真实 exe 路径；Windows 会忽略不能由 `subprocess` 直接执行的 cmd/ps1 shim |
| `AUTO_CODING_CLAUDE_MODEL` | `deepseek-v4-pro` | 传给 Claude Code 的模型名 |
| `AUTO_CODING_CLAUDE_TIMEOUT_SECONDS` | `600` | 单轮超时，最小 10 秒 |
| `AUTO_CODING_MAX_BUDGET_USD` | `None` | 可选单轮 Claude Code 预算参数，必须大于 0 |
| `AUTO_CODING_DATA_DIR` | `~/.autocoding-agent` | 会话与能力数据根目录 |
| `AUTO_CODING_INCIDENT_SQLITE_PATH` | `None` | 可选的异常诊断 SQLite 文件路径；连接始终以只读模式打开 |
| `AUTO_CODING_DATABASE_MAX_ROWS` | `100` | 两套流程每条查询的主机返回行数上限，范围 1–1000；模型契约最多请求 100 行 |
| `AUTO_CODING_DATABASE_QUERY_TIMEOUT_SECONDS` | `60` | SQL Server/SQLite 单条查询超时，范围 1–60 秒 |
| `AUTO_CODING_DATABASE_MAX_QUERY_ROUNDS` | `2` | 每个开发或异常会话最多自动查询轮次，范围 1–5 |
| `AUTO_CODING_AGENT_MAX_REPLAN_ROUNDS` | `2` | 验证失败后的最大重规划轮数，范围 1–10 |
| `AUTO_CODING_RUNTIME_LEASE_SECONDS` | `30` | 启动恢复扫描使用的本地运行租约秒数，范围 5–3600 |

`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` 等模型服务环境变量不由 `Settings` 解析，
但会由 Claude Code 子进程继承。桌面配置页把它们保存为 Windows 当前用户环境变量；API Key
不会进入 `ModelSetupState`，也不会回填到界面。不要把真实密钥提交到项目 `.env` 或能力文档。

## 11. 异常诊断接口

### 11.1 `IncidentApplication`

```python
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.sqlserver_service import SQLServerConnectionService

connections = SQLServerConnectionService()
reader = connections.reader()
incidents = build_incident_application(
    database=reader,
    database_reference=reader.reference if reader else None,
)
outcome = incidents.start(
    workspace=r"D:\repo",
    problem="订单 42 一直停留在处理中",
    page_hint="/orders/42",
    project="生物",
    source="manual",
    external_reference=None,
)
```

| 方法 | 行为 |
| --- | --- |
| `start(workspace, problem, page_hint=None, *, project=None, source="manual", external_reference=None)` | 创建异常会话，保存所选知识项目，定位页面并在必要时查询数据库 |
| `send(session_id, message, command_id=None)` | 回答澄清、补充异常上下文，或从 completed 开启下一诊断 cycle；command ID 可幂等重试 |
| `resume(session_id, action="read_only_inspect")` | 从 paused/recovery_required 明确继续或重新调查 |
| `cancel(session_id)` | 取消非终态异常诊断，不运行模型或写数据库 |
| `outcome(session_id)` | 返回最新 `IncidentOutcome` |
| `get_session(session_id)` | 返回完整 `IncidentSession`，但不包含原始数据库行 |
| `list_sessions()` | 按更新时间倒序列出异常会话 |
| `events(session_id)` / `runs(session_id)` | 返回可审计事件与 Runtime Run |

`source` 与 `external_reference` 为钉钉消息来源和外部消息/工单 ID 预留。当前调用是同步的；
`completed` 会话允许继续发送并生成新的逐轮异常能力文档。`recovery_scan` 给出本次应用启动
发现并暂停的孤儿异常任务。

### 11.2 状态与结构化决定

`IncidentStatus` 包含：

- `needs_input`：问题或页面信息不足，`question` 必填；
- `query_required`：需要页面映射或业务数据，至少一条 `queries` 必填；允许先查 Menu 等映射表，
  因而 `page` 可以暂时为空；
- `completed`：诊断结束，`page` 与 `diagnosis` 必填；
- `failed`：Runtime、契约或数据库边界失败。

模型返回的 `IncidentDecision` 主要字段为：

```text
status, message, question
page: LocatedPage | None
queries: list[DataQuery] (最多 5 条)
diagnosis: str | None
findings: list[IncidentFinding]
recommended_actions: list[str]
confidence: float | None (0..1)
automation_candidate: bool
```

`LocatedPage` 保存名称、可选 route、页面源码路径、相关后端路径和定位依据。所有源码路径必须
是安全的工作区相对路径。`DataQuery` 保存名称、用途、SQL、命名参数和 1–100 的请求行数，默认
为 100；数据库适配器仍会应用更小的主机上限。结果数量未知时，开发与异常 Prompt 都要求模型
先做 100 条有界采样并在 SQL 中使用适合方言的 `TOP`/`LIMIT`；已知少量结果时应使用更小限制。

`IncidentOutcome.query_observations` 只包含查询名称、用途、SQL 指纹、参数名、返回行数、截断标记
和脱敏列。SQL 参数值和原始业务行不会写入运行时数据库；结构化 SQL 只存在于当前模型决定和
执行调用中，完成后的持久化审计使用指纹。
`IncidentSession.database_reference` 保存该会话的无凭据数据源引用，例如
`sqlserver://server:1433/database`；它不保存数据库账号、密码或业务数据。
`IncidentSession` 与开发会话一样保存 `cycle_number/cycle_objective` 和当前轮查询审计起点；
`IncidentOutcome` 只返回本 cycle 的查询摘要，Session/Event 继续保留全部历史审计。

### 11.3 `DatabaseReader`

```python
class DatabaseReader(Protocol):
    def describe_schema(self) -> str: ...
    def execute(self, query: DataQuery) -> QueryResult: ...
```

桌面把同一个 `SQLServerDatabaseReader` 注入开发与异常流程，SQLite 适配器保留给异常 CLI
和旧会话。适配器必须使用
专用只读账号、强制超时/行数限制、拒绝多语句和写操作、参数绑定、脱敏结果，并只返回有限
schema 元数据。

### 11.4 异常 CLI

安装后入口为 `autocoding-incident`。当前 CLI 的 `--database` 仍是 SQLite 兼容入口；SQL Server
优先通过桌面连接页配置：

| 命令 | 作用 |
| --- | --- |
| `check-db --database PATH` | 验证 SQLite 只读连接并显示有限 schema |
| `start PROBLEM --workspace PATH [--page HINT] [--database PATH]` | 创建并运行异常调查 |
| `send MESSAGE --session-id UUID [--database PATH]` | 补充信息并恢复同一模型会话 |
| `resume --session-id UUID [--action read_only_inspect|replan|cancel]` | 显式恢复暂停任务 |
| `cancel --session-id UUID` | 取消非终态异常任务 |
| `show --session-id UUID` | 输出最新完整 JSON 结果 |
| `sessions` | 列出最近异常会话 |

未传 `--database` 时使用 `AUTO_CODING_INCIDENT_SQLITE_PATH`。如果模型请求数据但未配置数据库，
该会话会得到可持久化的 `failed` 结果，而不会把 SQL 交给用户或假装完成诊断。
