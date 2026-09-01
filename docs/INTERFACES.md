# AutoCoding Engineer 接口与数据契约

本文记录当前 `0.7.12` 已实现的软件开发、异常诊断、Python、CLI、桌面客户端、Streamlit、
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
    knowledge_retriever: KnowledgeRetriever | None = None,
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
- `knowledge_retriever` 可替换 RAG 检索实现；未传时使用当前明确标识的本地伪实现。

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
当前 Session 能力文档的创建或追加；新的 `send` 会增加 cycle 并重新进入只读轮次。`failed`
当前也可以通过 `send` 重新进入只读轮次。

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
  `decision_recorded/repaired`、`policy_repair_requested`、`artifact_recorded/failed`、
  `task_reopened`。
- `RecoveryAction`：`read_only_inspect`、`replan`、`cancel`。

## 3. 结构化模型契约

所有模型均为 Pydantic `BaseModel`。

### 3.1 消息、证据和审批

```text
ChatMessage
  role: MessageRole
  content: str
  attachments: list[MessageAttachment] = [] (最多 5 项)
  created_at: datetime (UTC 自动生成)
```

```text
MessageAttachment
  id: str (默认 UUID)
  kind: "image"
  path: non-empty str
  name: non-empty str
  media_type: non-empty str
  size_bytes: int (1..10 MiB)
```

附件仅表示主机显式附加到这条消息的本地证据，不从普通消息文本自动识别文件路径。异常 Engine
会在调用 Runtime 前重新解析并核对图片路径、后缀与大小；开发流程当前不接收附件。

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
  additional_dirs: list[str] = [] (最多 5 项)
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
  --safe-mode
  --no-chrome
  --strict-mcp-config
  --mcp-config '{"mcpServers":{}}'
  --setting-sources ''
  --output-format stream-json
  --input-format text
  --include-hook-events
  --model <configured-model>
  --permission-mode <permission-mode>
  --tools <comma-separated-tools>
  --allowedTools <one or more allowed tool patterns>
  --append-system-prompt-file <per-turn UTF-8 temporary file>
  --json-schema <AgentDecision JSON Schema>
  [--add-dir <workspace capability directory，仅 inspect>]
  [--add-dir <validated incident attachment directory> ...]
  [--max-budget-usd <amount>]
  (--session-id <application session id> | --resume <runtime session id>)
```

`RuntimeTurn.user_message` 不进入命令数组，而是通过 stdin 发送。临时文件包含本轮完整系统提示词，
只在子进程存活期间存在，正常完成、失败或超时后都会清理。子进程工作目录固定为
`RuntimeTurn.workspace`。开发可观测路径要求 stdout 是逐行 stream-json，
最终 `result` 行包含以下 JSON envelope；通用 `run_structured()` 兼容路径仍可直接读取最终 JSON：

Windows 运行时还会传入隐藏 `STARTUPINFO` 和 `CREATE_NO_WINDOW`，避免每轮 Claude Code 调用
弹出控制台。真实进程启动前会恢复失效的旧命令配置，并分别预检 `command[0]` 与
`RuntimeTurn.workspace`；最终采用的命令和工作区进入日志，完整 prompt 仍不会记录。注入测试
Runner/Popen 时跳过本机路径恢复，保留可替换 Runtime 的确定性测试边界。每次调用还会记录
`command_chars`、系统提示词/用户消息/JSON Schema 的字符数，并在 Windows `CreateProcess` 前
拒绝仍达到 32,767 字符的剩余参数；日志不包含这些输入的正文。

`--safe-mode` 与显式 `--tools/--allowedTools` 组合使用：前者阻止项目或用户自定义项改变本轮
行为，后者只开放当前状态允许的原生工具。可观测 Runtime 还会检查每个流式 `Glob/Grep`
调用：单轮组合预算为 8 次；Glob 不接受通配整个仓库或递归全扩展模式；目录级 Grep 必须设置
`glob/type` 和 `head_limit=1..100`；显式搜索路径必须位于 workspace、能力目录或附件目录。
违反策略会产生 `RuntimeEventKind.POLICY_BLOCKED`，审计中只保存工具名、脱敏后的范围信息和阻断
原因，不保存源码正文。`RuntimePolicyBlockedError` 还携带 policy、operation、脱敏 reason 和是否
允许纠正；inspect Engine 对可纠正错误最多自动重试一次，并产生 `policy_repair_requested`。修正
轮次仍复用同一 Runtime Session；不可纠正错误和第二次违规保持终止行为。implement/verify 不自动
重试，因为中断时可能存在副作用不确定性。

异常 `RuntimeTurn` 的工具列表按页面定位证据动态生成：当前 cycle 尚无成功且非空的
`page_lookup` observation 时为 `Read`；有候选后为 `Read,Glob,Grep`。成功但 0 行的精确页面查询
不会解锁源码搜索，模型应继续有界模糊页面查询。`policy_blocked` 活动会同时保存脱敏后的
`pattern/path/glob/type/output_mode/head_limit`，方便区分工具边界问题和业务调查问题。

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

`SQLiteTaskStore` 和 `SQLiteIncidentStore` 内部组合 `SQLiteRuntimeDatabase`。这个基础类统一：

| 方法 | 内部契约 |
| --- | --- |
| `connect()` | 打开同一个 `agent-runtime.db`，启用 foreign key、5000ms busy timeout 和 WAL |
| `safe_id(task_id)` | 只接受 UUID，阻止 ID 被当作路径或 SQL 标识符 |
| `read_json_records(...)` | 从受信任布局中的表按 task 和稳定顺序读取 JSON 记录 |
| `append_events(...)` | 分配连续 sequence，并拒绝修改已追加事件 |
| `upsert_runs(...)` | 只允许 started Run 更新 heartbeat 或进入一个不可改写终态 |
| `append_command_receipts(...)` | 保存全局幂等命令结果并拒绝覆盖 |
| `replay_state(...)` | 按转换事件回放状态，断链时报告 `EventStoreCorruption` |

表布局由代码内的 `DEVELOPMENT_LAYOUT` / `INCIDENT_LAYOUT` 提供并校验标识符；调用方不能把用户输入
作为表名传入。领域 Store 仍控制 snapshot、Decision/Artifact、旧 JSON 迁移和事务提交。

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

一个 Session 始终使用 `tasks/<session-id>.json`。共享字段和开发专有字段包括：

```text
schema_version
task_id
session_id
cycle_number
cycle_count
last_cycle_number
workspace_id
goal
cycle_objective
outcome
changed_files
test_summary
document
model
completed_at
created_at
updated_at
cycles
```

异常记录使用相同的 `session_id/cycle_number/cycle_objective/document/model/completed_at`，并以
`problem` 代替开发记录的 `goal`；开发记录另外保存 changed files 和 test summary。

`cycles` 是按 `cycle_number` 排序的完成历史；开发条目记录本轮目标、结果、变更文件和测试，
异常条目记录本轮目标、结果、诊断和建议动作。`document` 是相对于当前工作区能力目录的 POSIX
风格路径。主 task JSON 中已存在的 cycle 编号是追加幂等判断依据。

### 6.2 能力 Markdown

文档 frontmatter 包含 `schema_version`、`workflow`、`session_id`、`cycle_count`、
`last_cycle_number`、`model`、`created_at` 和 `updated_at`，正文包含：

- 标题和摘要；
- 适用场景；
- 方法；
- 验证；
- 风险与边界；
- 任务证据；
- 来源目标、结果和变更文件。

文件名始终为 `<session-id>.md`。首次完成写入完整正文；后续 cycle 完成时在同一文件追加
“后续工作轮次”或“后续诊断轮次”章节。同一 cycle 重复记录返回已有内容，不会重复追加。
文件名不使用模型生成的标题，从根本上避免标题里的敏感值通过文件名或索引链接泄漏。

`CAPABILITIES.md` 由 task JSON 重建，每个 Session 只生成一个链接，显示累计完成轮次和最新结果
前 240 个字符。该索引明确提示历史知识可能过期。v0.5.3 的旧逐轮记录在索引层按 Session 去重，
后续写入时可以折叠进主文档，旧源文件不自动删除。

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
- 项目代码根目录从“系统配置”读取，主对话区不重复显示；已有 Session 继续使用创建时的路径；
- 异常模式不显示独立页面栏，欢迎文案和输入占位提示提供页面标题或路径，也可以直接粘贴截图；
  模型先理解对话再分析图片，线索不足或图片与候选页面冲突时请求确认；输入框支持 `Ctrl+V`
  粘贴最多 5 张异常截图、清除待发送图片和纯图片发送；普通文本粘贴及开发模式不受影响；
- SQL Server 不重复占用输入区，统一从“系统配置”管理；
- 统一系统配置和本地滚动日志目录快捷入口；
- 新任务、持久化聊天记录及同一 session 的多轮补充；
- 对话记录整体保持只读但允许鼠标选取；支持 `Ctrl+C` 复制、`Ctrl+A` 全选，以及右键
  `复制所选文本` / `全选对话`，选区不会因打开右键菜单而丢失；选择标签位于消息正文背景之上，
  标题和正文都会显示相同的紫蓝色高亮；
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

系统配置是一个窗口、五个页签。模型页字段为 Claude Code 路径、API 地址、模型名称和 API
Key。Claude 路径会通过
`--version` 验证；Key 控件始终为空并以密码形式输入，`has_api_key=true` 时留空保存表示保留
原密钥。保存成功后当前进程立即生效，并重建两套 Runtime，但不删除已有会话。

Embedding 页字段为 API 地址、模型名称、输出维度和 API Key，默认分别为
`https://api.voyageai.com/v1/embeddings`、`voyage-code-4` 和 1024。API Key 不回填，留空保存
表示保留系统凭据；“测试连接”在后台发送一条 query embedding 并校验返回维度。保存后的配置
用于知识库管理和两套流程的新任务，活动任务保持原 Retriever。端点、模型或维度变化会产生新的
索引 ID，不自动复用或删除旧索引。

SQL Server 页包含服务器、端口、数据库、已安装 ODBC 驱动、Windows/SQL Server 认证、
用户名、密码、加密和信任证书选项，并提供后台“测试连接”。非密钥字段保存在
`<data_dir>/database/sqlserver.json`；密码通过 Windows Credential Manager 保存，不进入 JSON、
日志、模型提示词或会话。两套流程共享连接；连接可随时更换，已有开发或异常会话保持原连接，
新配置从对应流程的下一项任务开始。

“项目路径”页保存开发与异常处理新任务共用的代码根目录。`WorkspaceConfigService.save()` 只接受
当前存在且可访问的目录，规范路径以 JSON 原子写入 `<data_dir>/workspace/project.json`。配置
切换不会改写已有 Session 中的 workspace；配置缺失或路径已不可访问时，桌面端阻止新任务并
引导用户返回该页重新选择。

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
| `AUTO_CODING_CLAUDE_COMMAND` | 自动发现，最终回退 `claude` | Claude Code 可执行命令或真实 exe 路径；Windows 启动脚本用最新用户值刷新进程，Runtime 会跳过失效路径并忽略不能由 `subprocess` 直接执行的 cmd/ps1 shim |
| `AUTO_CODING_CLAUDE_MODEL` | `deepseek-v4-pro` | 传给 Claude Code 的模型名 |
| `AUTO_CODING_CLAUDE_TIMEOUT_SECONDS` | `600` | 单轮超时，最小 10 秒 |
| `AUTO_CODING_MAX_BUDGET_USD` | `None` | 可选单轮 Claude Code 预算参数，必须大于 0 |
| `AUTO_CODING_DATA_DIR` | `~/.autocoding-agent` | 会话与能力数据根目录 |
| `AUTO_CODING_INCIDENT_SQLITE_PATH` | `None` | 可选的异常诊断 SQLite 文件路径；连接始终以只读模式打开 |
| `AUTO_CODING_DATABASE_MAX_ROWS` | `100` | 两套流程每条查询的主机返回行数上限，范围 1–1000；模型契约最多请求 100 行 |
| `AUTO_CODING_DATABASE_QUERY_TIMEOUT_SECONDS` | `60` | SQL Server/SQLite 单条查询超时，范围 1–60 秒 |
| `AUTO_CODING_DATABASE_MAX_QUERY_ROUNDS` | `2` | 每个开发 cycle 最多自动查询轮次，范围 1–5 |
| `AUTO_CODING_INCIDENT_MAX_PAGE_QUERY_ROUNDS` | `2` | 每个异常 cycle 最多成功页面定位查询轮次，范围 1–5 |
| `AUTO_CODING_INCIDENT_MAX_BUSINESS_QUERY_ROUNDS` | `2` | 每个异常 cycle 最多成功业务数据查询轮次，范围 1–5 |
| `AUTO_CODING_INCIDENT_MAX_QUERY_REPAIR_ROUNDS` | `1` | 每个异常 cycle 最多 SQL 纠错轮次，范围 0–3 |
| `AUTO_CODING_AGENT_MAX_REPLAN_ROUNDS` | `2` | 验证失败后的最大重规划轮数，范围 1–10 |
| `AUTO_CODING_RUNTIME_LEASE_SECONDS` | `30` | 启动恢复扫描使用的本地运行租约秒数，范围 5–3600 |
| `AUTO_CODING_HERMES_SKILLS_ENABLED` | `true` | 是否自动发现并启用可选 Hermes Skill 服务 |
| `AUTO_CODING_HERMES_COMMAND` | 自动发现 | Hermes CLI 可执行路径；依次检查显式配置、PATH 与 `HERMES_HOME/bin` |
| `AUTO_CODING_HERMES_HOME` | `HERMES_HOME` 或 `~/.hermes` | Hermes 数据和 Skill 根目录 |
| `AUTO_CODING_HERMES_USE_ACE_PROVIDER` | `true` | 是否沿用 ACE 的 DeepSeek 地址/API Key；关闭后使用 Hermes 自有 provider 配置 |
| `AUTO_CODING_HERMES_MODEL` | `deepseek-v4-flash` | 启用 ACE provider 桥接时传给 Hermes 的独立模型名 |
| `AUTO_CODING_HERMES_SKILL_ALLOWED_CATEGORIES` | `software-development,github,research` | 允许暴露给模型的 Skill 分类，逗号分隔 |
| `AUTO_CODING_HERMES_SKILL_TIMEOUT_SECONDS` | `120` | 单次咨询超时，范围 10–600 秒 |
| `AUTO_CODING_HERMES_SKILL_MAX_OUTPUT_CHARS` | `12000` | 脱敏后回灌文本上限，范围 1000–16000 字符 |
| `AUTO_CODING_HERMES_SKILL_MAX_TURNS` | `4` | Hermes 单次咨询最多工具循环，范围 1–12 |
| `AUTO_CODING_HERMES_SKILL_MAX_ROUNDS` | `1` | 单个用户命令最多咨询次数，当前范围 1–2 |

`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` 等模型服务环境变量不由 `Settings` 解析，
但会由 Claude Code 子进程继承。桌面配置页把它们保存为 Windows 当前用户环境变量；API Key
不会进入 `ModelSetupState`，也不会回填到界面。Hermes 桥接只在调用时从同一用户配置读取地址和
密钥，将密钥映射到子进程专用 `DEEPSEEK_API_KEY`，不进行第二次持久化。不要把真实密钥提交到
项目 `.env` 或能力文档。

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
    page_hint="订单详情",
    project="生物",
    source="manual",
    external_reference=None,
    attachments=[],
)
```

| 方法 | 行为 |
| --- | --- |
| `start(workspace, problem, page_hint=None, *, project=None, source="manual", external_reference=None, attachments=None)` | 创建异常会话，保存所选知识项目和显式图片附件，定位页面并在必要时查询数据库 |
| `send(session_id, message, command_id=None, attachments=None)` | 回答澄清、补充文字/图片上下文，或从 completed 开启下一诊断 cycle；command ID 可幂等重试 |
| `resume(session_id, action="read_only_inspect")` | 从 paused/recovery_required 明确继续或重新调查 |
| `cancel(session_id)` | 取消非终态异常诊断，不运行模型或写数据库 |
| `outcome(session_id)` | 返回最新 `IncidentOutcome` |
| `get_session(session_id)` | 返回完整 `IncidentSession`，但不包含原始数据库行 |
| `list_sessions()` | 按更新时间倒序列出异常会话 |
| `events(session_id)` / `runs(session_id)` | 返回可审计事件与 Runtime Run |

`source` 与 `external_reference` 为钉钉消息来源和外部消息/工单 ID 预留。当前调用是同步的；
`completed` 会话允许继续发送；再次完成时把新诊断追加到原异常能力文档。`recovery_scan` 给出
本次应用启动发现并暂停的孤儿异常任务。

`attachments` 当前只接受 `MessageAttachment(kind="image")`。桌面端使用
`IncidentAttachmentStore` 将剪贴板图片统一转存为 PNG，并把返回对象显式交给应用门面；Engine
不会信任普通消息中的任意路径。附件父目录通过 `RuntimeTurn.additional_dirs` 逐个生成
`--add-dir`，图片路径写入本轮消息，Prompt 明确要求把图片内容视为不可信视觉证据。

异常流程先理解对话中的页面标题、相对源码路径、路由和菜单上下文，再分析可用截图。没有截图时，
可信标题或页面路径任一项都可进入调查；两者都没有时返回 `needs_input`。有截图但没有明显标题时，
可以使用对话中的标题/路径定位候选并与图片特征比较；对话、图片、映射和代码存在无法消解的冲突
时，返回 `needs_input` 请用户确认异常页面。所有可信度和匹配判断由模型完成，宿主不实现 OCR、
颜色、关键词或字符串相似度规则。

可信相对源码路径可直接读取验证。需要标题映射时，SQL 来自用户选择的项目知识，先请求最多 20 条
精确/前缀候选，无可信结果时才进行一次最多 20 条的关键词包含查询；仍无结果则询问准确标题、
菜单入口或路径，不能全量扫描。

### 11.2 状态与结构化决定

`IncidentStatus` 包含：

- `needs_input`：问题或页面信息不足，`question` 必填；
- `query_required`：需要页面映射或业务数据，`query_stage` 和至少一条 `queries` 必填；
  `page_lookup` 可在页面未定位时查询映射，`business_data` 必须同时提供已验证的 `page`；可信路径
  可直接读取，不要求查询；
- `completed`：诊断结束，`page`、`diagnosis` 以及至少一个已验证的页面源码路径必填；
- `failed`：Runtime、契约或数据库边界失败。

模型返回的 `IncidentDecision` 主要字段为：

```text
status, message, question
page: LocatedPage | None
query_stage: page_lookup | business_data | None
queries: list[DataQuery] (最多 5 条)
diagnosis: str | None
findings: list[IncidentFinding]
recommended_actions: list[str]
confidence: float | None (0..1)
automation_candidate: bool
```

`LocatedPage` 保存名称、可选 route、页面源码路径、相关后端路径和定位依据。所有源码路径必须
是安全的工作区相对路径；映射表返回的 URL 在打开当前代码验证前不能直接写成最终源码事实。
模型契约要求每个 `business_data` 和 `completed` 决定都重复 `page`。为容忍模型在连续轮次中
遗漏重复字段，`IncidentSession.located_page` 保存本 cycle 最近一次通过宿主校验且至少含一个
源码路径的页面；后续决定漏传 `page` 时宿主可以恢复该对象并记录 `decision_repaired`。这不是
放宽页面前置条件：没有已验证页面、路径为空/越界或进入新 cycle 时都不能自动补全。
`DataQuery` 保存名称、用途、SQL、命名参数和 1–100 的请求行数，默认
为 100；参数契约优先使用 `:name`，参数字典键写不带前缀的 `name`。SQL Server 适配器还安全
兼容 `@name`，统一转换成 ODBC `?` 后按出现顺序独立绑定，绝不插值。数据库适配器仍会应用更小的主机上限。结果数量未知时，开发与异常 Prompt 都要求模型
先做 100 条有界采样并在 SQL 中使用适合方言的 `TOP`/`LIMIT`；已知少量结果时应使用更小限制。

`IncidentOutcome.query_observations` 只包含查询名称、用途、阶段、成功/失败状态、脱敏错误、SQL
指纹、参数名、返回行数、截断标记和脱敏列。SQL 参数值和原始业务行不会写入运行时数据库；结构化 SQL 只存在于当前模型决定和
执行调用中，完成后的持久化审计使用指纹。
`IncidentSession.database_reference` 保存该会话的无凭据数据源引用，例如
`sqlserver://server:1433/database`；它不保存数据库账号、密码或业务数据。
`IncidentSession` 与开发会话一样保存 `cycle_number/cycle_objective` 和当前轮查询审计起点；另外
保存总尝试数 `query_rounds`、成功页面查询数 `page_query_rounds`、成功业务查询数
`business_query_rounds` 和 SQL 失败纠错数 `query_repair_rounds`。四项在 completed 会话续聊进入
新 cycle 时重置，同时清空 `located_page`，但历史 Observation/Event 不清空；
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

## 12. RAG 知识接口

### 12.1 可替换端口

`knowledge_rag/ports.py` 定义三个运行时无关协议：

```python
class EmbeddingProvider(Protocol):
    model_id: str
    dimension: int
    simulated: bool
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, query: str) -> list[float]: ...

class VectorStore(Protocol):
    def replace_document(self, document_id: str, points: list[VectorPoint]) -> None: ...
    def delete_document(self, document_id: str) -> None: ...
    def search(self, vector: list[float], *, domain, project, workspace_id, limit): ...

class KnowledgeRetriever(Protocol):
    def retrieve(self, query: str, *, domain, project=None, workspace_id=None, limit=6): ...
```

`FakeEmbeddingProvider` 的 `model_id` 固定为 `fake-hash-embedding-v1`、维度为 96、
`simulated=True`，只在 Voyage 未配置时使用；同样文本得到相同向量，但不承诺语义相似度。
`VoyageEmbeddingProvider` 的 `model_id` 由 provider、模型、维度和 endpoint 指纹组成，
`simulated=False`。两者遵守相同端口但使用不同数据库和索引身份。

### 12.2 Voyage 配置与 REST 契约

`EmbeddingConnectionConfig` 是不含密钥的 Pydantic 模型：

```text
provider = "voyage"
endpoint
model
output_dimension
request_timeout_seconds (1..60)
index_id / model_id（派生，只用于索引隔离）
```

`EmbeddingConfigStore` 把配置原子写入 `<data_dir>/embedding/voyage.json`，API Key 使用 keyring
保存到 OS 凭据管理器。`EmbeddingSetupState` 只返回 `config` 和 `has_api_key`，不提供密钥字段。
`EmbeddingSetupService` 提供 `inspect/defaults/build_config/save/test/provider`。

Voyage Adapter 发送：

```http
POST <configured endpoint>
Authorization: Bearer <secret>
Content-Type: application/json

{
  "input": ["..."],
  "model": "voyage-code-4",
  "input_type": "document | query",
  "truncation": true,
  "output_dimension": 1024,
  "output_dtype": "float"
}
```

文档最多按 128 条分批；响应 `data` 必须数量一致、index 连续且每个向量包含配置维度的有限数字。
HTTP、网络和契约错误转换成不含 Authorization/API Key 的 `VoyageEmbeddingError`。

### 12.3 文档与 Chunk 契约

`KnowledgeDocument` 保存源路径、展示路径、标题、来源类型、领域、项目、工作区、当前/已索引
Hash、索引状态、Chunk 数量、Embedding 模型和时间。状态为：
`pending/indexing/indexed/outdated/failed/removed`。来源类型为：
`project_knowledge/engineering_experience/capability/failure_knowledge`；当前发现器已接入前三类，
Failure Knowledge 只预留模型值。

`KnowledgeChunk` 保存稳定 ID、文档 ID、序号、标题与 heading path、正文、embedding text、
内容 Hash、近似 token 数、领域/项目/工作区/来源和源路径。Embedding text 在正文前增加文档、
来源、领域、项目和标题元数据，但 FTS 与返回 Prompt 使用可读 Chunk 正文。

### 12.4 `KnowledgeRAGService`

| 方法 | 行为 |
| --- | --- |
| `refresh_documents()` | 发现 Project Knowledge、工程经验和开发/异常 Capability；同步状态但不自动索引 |
| `preview_chunks(document_id)` | 只做 Markdown 分块预览，不写索引 |
| `index_document(document_id)` | 使用当前 Embedding 重建该文档的向量、Chunk 和 FTS5，并返回 `KnowledgeIndexReceipt` |
| `remove_document(document_id)` | 删除该文档的 Chunk/FTS/向量记录，保留源 Markdown |
| `retrieve(query, domain, project=None, workspace_id=None, limit=6)` | Dense + BM25 + RRF 混合检索，返回带来源的 `KnowledgeRetrievalResult` |

`build_fake_rag_service(settings, project_root=...)` 使用
`<data_dir>/rag/knowledge-fake.db`。`build_voyage_rag_service(...)` 使用
`<data_dir>/rag/knowledge-voyage-<index-id>.db`；`build_configured_rag_service(...)` 在 Voyage 已配置
时选择正式服务，否则选择 Fake。所有索引都是派生数据，可从源 Markdown 全量重建。

### 12.5 Agent 注入与事件

开发 Engine 只在 `inspect` 模式调用 `KnowledgeRetriever`；异常 Engine 在每个用户调查 cycle 开始
检索一次，并把同一上下文沿数据库查询循环继续传给模型。命中内容置于
`<retrieved_knowledge>` 中，并明确声明为不可信、可能过期的参考。

- `knowledge_retrieved`：记录命中数、模型 ID、是否模拟，以及 Chunk ID/源路径；空结果也记录；
- `knowledge_retrieval_failed`：记录脱敏截断后的错误，主任务继续执行；
- 当前不把向量、完整 Chunk 正文或用户查询复制进 Event Store。

桌面“知识库管理”提供刷新、分块预览、多选加入/重建、移除和测试检索。模式徽标会明确显示
“模拟模式”或当前正式 Voyage 模型 ID；任务完成生成的新 MD 只成为待加入文档。

## 13. 实时进度接口

```python
class ProgressEvent(BaseModel):
    task_id: str | None
    workflow: ProgressWorkflow
    phase: ProgressPhase
    label: str
    detail: str | None
    active: bool
    created_at: datetime

ProgressSink = Callable[[ProgressEvent], None]
```

`AgentApplication` 和 `IncidentApplication` 的 `start()`、`send()`、`resume()`、`cancel()` 以及开发
流程的 `approve()`、`reject()` 均接受关键字参数 `progress_sink`。调用方可以省略它，保持现有
CLI/Web/测试兼容。Engine 使用 `emit_progress()` 调用回调；回调抛出的异常只记录警告，不传播到
任务线程。

`ProgressProjector.from_runtime()` 只接受脱敏后的 `RuntimeActivity`，按工作流、执行模式和已验证
附件路径投影阶段。它不会复制工具输入，也不会把任意模型文本当作主状态。桌面端通过线程安全
结果队列接收事件，相同阶段更新详情，不同阶段采用最小可见时间和轻量文本渐变。

## 14. Hermes Skill 接口

### 14.1 端口与结构化模型请求

```python
class HermesSkillService(Protocol):
    def available_skills(self) -> list[HermesSkillSummary]: ...
    def invoke(self, request: HermesSkillRequest) -> HermesSkillResult: ...
```

`AgentDecision` 与 `IncidentDecision` 增加内部状态 `hermes_skill_required` 和可空字段
`hermes_skill`。只有该状态必须提供 `HermesSkillRequest(skill, question, reason)`，其他状态携带该
字段会被 Pydantic 拒绝。Engine 只在 inspect 阶段接受此状态；它不会映射为新的 `TaskState`，而是
在当前 `inspecting` 状态内完成一次外部咨询并继续同一 Claude Runtime session。

### 14.2 发现与执行契约

`HermesCliSkillService` 只扫描：

```text
<HERMES_HOME>/skills/<allowed-category>/<exact-skill>/SKILL.md
```

分类来自宿主配置；Skill 目录名必须符合小写安全 slug，frontmatter 的 `name` 必须与目录一致，
路径必须解析在 skills 根目录内。模型只看到名称、分类和最多 500 字的描述，不接收完整 Skill
Markdown。调用参数固定包含：

```text
hermes chat --query-file - --toolsets web --skills <exact-name>
            --max-turns 4 --quiet --ignore-rules --source tool
            --model deepseek-v4-flash --provider custom
```

问题通过 stdin 传入，cwd 固定为 `HERMES_HOME`，超时和输出长度由 Settings 限制；Windows 使用
隐藏子进程参数，并显式使用 UTF-8 管道编码。启用默认桥接时，`CUSTOM_BASE_URL` 指向经 HTTPS
主机校验的 DeepSeek `/anthropic`，ACE Key 仅放入子进程 `DEEPSEEK_API_KEY`；未知 Skill、错误
端点或缺失密钥都在模型调用前拒绝。

### 14.3 结果、事件、Artifact 与降级

`HermesSkillObservation` 保存 Skill、completed/failed、脱敏输出或错误、耗时和 Artifact ID。
开发与异常 Session/Outcome 均返回当前 cycle 的 observation；异常应用也新增 `artifacts()`。

- `hermes_skill_requested`：模型选择一个目录 Skill；
- `hermes_skill_completed`：收到脱敏、有界候选建议；
- `hermes_skill_failed`：缺失、超时、进程或模型配置失败；
- `hermes_skill_result` Artifact：保存脱敏 observation，`host_verified=false`。

调用失败不把任务置为 failed；Engine 把脱敏错误回给 Claude，要求依靠当前代码、RAG 和授权数据
继续。只有模型在本命令预算耗尽后仍重复请求 Hermes，才作为结构化协议违例结束该命令，避免
无限 Agent 循环。

## 15. 搜索策略纠正周期

开发与异常 Engine 在只读调查中维护 `consecutive_search_repair_rounds`。收到可纠正的
`RuntimePolicyBlockedError` 后写入 `policy_repair_requested` 并重试；收到任意通过结构校验的
Runtime 决策后计数归零。`MAX_SEARCH_REPAIR_ROUNDS=1` 因而约束的是一次连续违规链，而不是包含
多轮页面查询、源码调查和业务数据查询的整个命令。不可纠正阻断以及同一链的第二次阻断仍直接
进入失败状态。

## 16. 异常附件发送与完成响应

`IncidentAttachmentStore.prepare_for_send(attachment)` 只接受该 Store 隔离根目录中与附件 ID
严格匹配的 `incident-screenshot.png`。它验证 PNG 内容、尺寸与 10 MiB 上限，并返回包含当前绝对
路径和实际 `size_bytes` 的新 `MessageAttachment`；不会修改用户项目或原始剪贴板内容。桌面端在
每次新建或继续异常对话前调用该方法，验证失败时保留待发附件并在状态栏提示重新粘贴。

`IncidentOutcome.message` 和完成轮次的最终 Assistant 消息由宿主渲染为：

```text
结论
<一句话结论>

为什么出现这个异常
<证据支持的因果说明>

解决方法
1. <具体修复或验证动作>

结论置信度
<百分比或模型未量化>
```

结构化字段 `diagnosis`、`recommended_actions` 和 `confidence` 仍分别保留，供 API、能力文档与审计
使用。桌面元数据区域同步使用“为什么出现这个异常”和“解决方法”标签。

## 17. 紧凑数据库上下文与异常续聊契约

`compact_database_context(configured, reference)` 是开发和异常 Engine 共用的 Prompt 适配器。它
只返回数据库方言、按需读取元数据的方法和只读校验声明，不调用 `DatabaseReader.describe_schema()`。
完整数据库访问仍通过既有 `DataQuery`、`DatabaseReader.execute()` 和查询审计接口完成。

异常完成后无新附件的 `send()` 先请求：

```python
class IncidentContinuationDecision(BaseModel):
    status: Literal["answer", "investigate"]
    message: str
    diagnosis: str | None
    recommended_actions: list[str]
    confidence: float | None
```

`answer` 必须提供 `diagnosis`，并使用已有 `LocatedPage` 形成标准完成结果；`investigate` 只表达需要
深度调查的原因。该路由使用新的 Claude session、空 `history`、`tools=[]` 和
`allowed_tools=[]`，不会覆盖主调查的 `session.runtime_session_id`。有新图片时绕过紧凑路由，
直接进入完整图片/页面调查。Claude CLI 命令构造器以 `--tools ""` 明确关闭工具，并且只有非空
列表才发送 `--allowedTools`。
