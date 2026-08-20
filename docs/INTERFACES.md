# AutoCoding Engineer 接口与数据契约

本文记录当前 `0.2.0` 已实现的 Python、CLI、Streamlit、Runtime、持久化和状态契约。
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
) -> AgentApplication
```

- 未传 `settings` 时使用进程缓存的 `get_settings()`。
- 未传 `runtime` 时创建 `ClaudeCodeRuntime`。
- 创建数据目录，并使用 JSON 会话存储和文件型能力存储。
- `runtime` 参数可用于测试或替换模型执行适配器。

### 1.2 `AgentApplication`

`AgentApplication` 是 CLI、UI 和其他调用方应使用的稳定门面。

| 方法 | 输入 | 行为和返回 |
| --- | --- | --- |
| `start(workspace, message)` | `str | Path`, `str` | 新建任务并执行首个只读轮次，返回 `AgentOutcome` |
| `send(session_id, message)` | `str`, `str` | 补充澄清或修订指令，以只读模式继续 |
| `approve(session_id)` | `str` | 批准当前请求的精确 scope，并以对应模式继续 |
| `reject(session_id, reason="")` | `str`, `str` | 拒绝当前请求，以只读模式继续并要求替代方案 |
| `outcome(session_id)` | `str` | 返回最近一次持久化结果 |
| `get_session(session_id)` | `str` | 返回完整 `AgentSession`，包含消息和事件 |
| `list_sessions()` | 无 | 按 `updated_at` 倒序返回所有会话 |

主要前置条件：

- `start` 的工作区必须真实存在且为目录，任务消息不能为空；
- `send` 的消息不能为空，且不能继续已 `completed` 的任务；
- `approve`、`reject` 只适用于存在 `pending_approval` 的会话；
- session ID 必须是已保存的 UUID。

这些方法当前是同步、阻塞调用。它们可能抛出 `ValueError`、`KeyError`、路径解析异常或存储
异常；模型执行期间的多数 Runtime/契约/策略异常会由 Engine 转换成 `failed` Outcome。

## 2. 核心枚举

### 2.1 `AgentStatus`

| 值 | 含义 |
| --- | --- |
| `needs_input` | 信息不足，需要用户再提供一条消息 |
| `approval_required` | 需要用户批准修改或验证范围 |
| `completed` | 当前任务已如实到达终态 |
| `failed` | Runtime、输出契约或策略检查失败 |

只有 `completed` 会禁止后续 `send`，并触发能力文档保存。`failed` 当前可以通过 `send`
重新进入只读轮次。

### 2.2 `AgentMode`

| 值 | 用途 |
| --- | --- |
| `inspect` | 澄清、搜索、阅读和诊断 |
| `implement` | 用户批准后编辑或写入工作区文件 |
| `verify` | 用户批准后执行白名单内的验证命令 |

### 2.3 其他枚举

- `ApprovalScope`：`modify`、`verify`。
- `MessageRole`：`user`、`assistant`、`system`；当前 Engine 追加的是 user/assistant。
- `EventType`：`turn_started`、`runtime_finished`、`input_required`、
  `approval_required`、`task_completed`、`task_failed`、`capability_saved`、
  `capability_failed`。

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
ApprovalRequest
  scope: ApprovalScope
  reason: str
  proposed_actions: list[str] = []
```

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
  evidence: list[Evidence] = []
  next_actions: list[str] = []
  approval: ApprovalRequest | None = None
  changed_files: list[str] = []
  test_summary: str | None = None
  capability: CapabilityDraft | None = None
```

这是 Claude Code 可以返回的唯一业务决定格式，也是传给 `--json-schema` 的 Schema 来源。

契约约束：

- `status=approval_required` 时必须存在 `approval`；
- 其他状态不允许携带 `approval`；
- `inspect` 模式不允许非空 `changed_files`；
- 所有 `changed_files` 和非空 evidence path 必须是安全的相对路径。

`message` 是用户可见的 Markdown 文本。`next_actions`、`evidence`、`changed_files` 和
`test_summary` 是机器可读补充；当前 CLI/UI 主要展示 `message`，不会单独完整渲染所有字段。

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
  type: EventType
  message: str
  data: dict[str, Any] = {}
  created_at: datetime (UTC 自动生成)
```

事件按追加顺序保存在 session 中。一次成功 Runtime 轮次通常产生：

```text
turn_started
runtime_finished
input_required | approval_required | task_completed | task_failed
[capability_saved | capability_failed，仅 completed]
```

Runtime 或策略异常的轮次会有 `turn_started` 和 `task_failed`，没有
`runtime_finished`。

### 3.5 `AgentSession`

```text
AgentSession
  id: str (UUID 自动生成)
  workspace: str
  goal: str
  runtime_session_id: str | None = None
  status: AgentStatus | None = None
  pending_approval: ApprovalRequest | None = None
  last_decision: AgentDecision | None = None
  last_usage: AgentUsage = empty usage
  capability_document: str | None = None
  messages: list[ChatMessage] = []
  events: list[AgentEvent] = []
  created_at: datetime
  updated_at: datetime
```

`id` 是应用会话 ID；`runtime_session_id` 是 Claude Code 返回、用于下一轮 `--resume` 的 ID。
两者不能假定始终相同。`workspace` 以严格解析后的绝对目录保存。

### 3.6 `AgentOutcome`

```text
AgentOutcome
  session_id: str
  workspace: str
  status: AgentStatus
  message: str
  evidence: list[Evidence] = []
  next_actions: list[str] = []
  approval: ApprovalRequest | None = None
  changed_files: list[str] = []
  test_summary: str | None = None
  capability_document: str | None = None
  usage: AgentUsage
  events: list[AgentEvent] = []
```

这是每次应用操作的公开返回值。`events` 当前包含该会话至今的完整事件列表，而不是仅本轮
增量。`capability_document` 是状态目录中的绝对路径，不是目标仓库内路径。

## 4. Runtime 端口与 Claude Code 契约

### 4.1 `AgentRuntime`

```python
class AgentRuntime(Protocol):
    def run(self, turn: RuntimeTurn) -> RuntimeResult: ...
```

自定义 Runtime 必须接收完整 `RuntimeTurn` 并返回经契约表达的 `RuntimeResult`。

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

### 4.2 Claude Code 命令

默认适配器构造的命令等价于：

```text
claude -p
  --bare
  --no-chrome
  --strict-mcp-config
  --mcp-config '{"mcpServers":{}}'
  --setting-sources ''
  --output-format json
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

子进程工作目录固定为 `RuntimeTurn.workspace`。适配器要求 stdout 是一个 JSON envelope：

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

默认 `JsonSessionStore` 把每个会话保存为：

```text
<data_dir>/sessions/<uuid>.json
```

- `create` 在 UUID 已存在时抛出 `FileExistsError`；
- `load` 在 UUID 格式非法时抛出 `ValueError`，不存在时抛出 `KeyError`；
- `save` 采用临时文件替换；
- `list` 读取全部 JSON 并按更新时间倒序排列。

JSON 内容是 `AgentSession.model_dump(mode="json")` 的完整结果。

## 6. 能力存储接口与文件格式

`CapabilityStore` 当前由 Engine 直接调用，没有单独的 Protocol。

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

`tasks/<session-id>.json` 当前包含：

```text
schema_version
task_id
session_id
workspace_id
goal
outcome
changed_files
test_summary
document
model
completed_at
```

`document` 是相对于当前工作区能力目录的 POSIX 风格路径。task JSON 的存在也是当前
幂等判断依据。

### 6.2 能力 Markdown

文档 frontmatter 包含 `schema_version`、`session_id`、`model`、`completed_at`，正文包含：

- 标题和摘要；
- 适用场景；
- 方法；
- 验证；
- 风险与边界；
- 任务证据；
- 来源目标、结果和变更文件。

文件名为 `<session-id>.md`。文件名不使用模型生成的标题，从根本上避免标题里可能出现的
敏感值通过文件名或索引链接泄漏。

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
| `send MESSAGE --session-id UUID` | `MESSAGE` 必填；`-s/--session-id` 必填 | 继续现有任务 |
| `approve --session-id UUID` | `-s/--session-id` 必填 | 批准待处理权限 |
| `reject --session-id UUID [--reason TEXT]` | session 必填；`-r/--reason` 可选 | 拒绝权限并继续 |
| `show --session-id UUID` | `-s/--session-id` 必填 | 显示最后结果 |
| `sessions` | 无 | 列出最近会话 |

示例：

```powershell
autocoding-agent start "调查上传失败原因" --workspace D:\repo
autocoding-agent send "入口是 src/upload.py" --session-id <uuid>
autocoding-agent approve --session-id <uuid>
autocoding-agent reject --session-id <uuid> --reason "先不要修改"
autocoding-agent show --session-id <uuid>
autocoding-agent sessions
```

`start/send/approve/reject/show` 的标准输出格式为：

```text
session: <uuid>
status: <status>
<model message>
[approval: <scope> — <reason>]
[capability: <absolute document path>]
```

`sessions` 输出 JSON 数组，每项包含 `session_id`、`status`、`workspace`、`goal`、
`updated_at`。接口操作抛错时 CLI 向 stderr 输出 `Error: ...` 并以退出码 1 结束。

## 8. Streamlit UI

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

首次消息调用 `start`，已有会话中的消息调用 `send`。完成后页面停止接收该任务的新消息，
用户需点击“新建任务”。UI 当前没有独立展示完整 evidence、usage、event timeline，也没有
流式 token 或后台任务接口。

## 9. 环境配置

`Settings` 使用 `.env` 和环境变量，前缀为 `AUTO_CODING_`。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTO_CODING_CLAUDE_COMMAND` | 自动发现，最终回退 `claude` | Claude Code 可执行命令或真实 exe 路径；Windows 会忽略不能由 `subprocess` 直接执行的 cmd/ps1 shim |
| `AUTO_CODING_CLAUDE_MODEL` | `deepseek-v4-pro` | 传给 Claude Code 的模型名 |
| `AUTO_CODING_CLAUDE_TIMEOUT_SECONDS` | `600` | 单轮超时，最小 10 秒 |
| `AUTO_CODING_MAX_BUDGET_USD` | `None` | 可选单轮 Claude Code 预算参数，必须大于 0 |
| `AUTO_CODING_DATA_DIR` | `~/.autocoding-agent` | 会话与能力数据根目录 |

`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` 等模型服务环境变量不由 `Settings` 解析，
但会由 Claude Code 子进程继承。不要把真实密钥提交到项目 `.env` 或能力文档。
