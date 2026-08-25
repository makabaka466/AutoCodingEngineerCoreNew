# 任务记录：ACE-RUNTIME-001 AgentEngine 状态机与可恢复执行架构升级

- 任务编号：`ACE-RUNTIME-001`
- 当前状态：`completed`
- 下一步责任方：维护方按新迭代规则选择后续任务
- 创建日期：2026-08-25
- 短状态快照：开发 Agent Runtime 的 Phase 0–6 已完成并通过 113 项完整回归。状态机、Handler、
  SQLite Task/Event、Decision、Artifact、stream-json Runtime 活动、命令幂等、run lease、启动
  恢复扫描、RecoveryRequired、重规划上限、CLI 与桌面恢复入口均已接通。异常流程仍保持独立
  只读状态机，复用通用 Event/Recovery 基础设施作为后续独立迭代，不影响本任务的开发流程验收。

## 1. 用户目标

将当前 AgentEngine 从“以结果状态字段推进流程”升级为具备以下能力的软件工程 Agent Runtime：

- 可控执行；
- 可暂停和恢复；
- 可追踪审计；
- 可解释决策；
- 失败分类与安全恢复；
- 为未来长任务和多 Agent 协作提供基础。

## 2. 当前事实

### 已有基础

- `AgentSession` 已持久化消息、模型 session ID、最后决定、审批、使用量和事件；
- `AgentStatus` 已表达 needs_input、query_required、approval_required、completed、failed；
- `AgentMode` 已把 Inspect、Implement、Verify 权限分开；
- `AgentEvent` 已记录 turn、runtime、审批、查询、完成、失败和 Capability 保存；
- `JsonSessionStore` 已使用 UUID 文件名和原子 replace；
- 首轮 Claude session ID 在 Runtime 启动前保存，后续精确 resume；
- 关闭并重启客户端后，已有 `WAIT_APPROVAL` 语义实际上可以通过持久化 session 继续；
- `AgentDecision.evidence`、`ChangeProposal` 和 Capability 已形成审计雏形。

### 真实缺口

- `session.status` 仍由 Engine 直接赋值，转换规则分散；
- 当前 Event 是 session 内可变数组，不是独立、追加式、带 sequence 的 Event Store；
- 状态快照和事件没有原子一致性、版本号和并发保护；
- Runtime 只返回最终 JSON，不能可靠观察 ToolUse、真实修改和测试命令；
- 没有 run lease、heartbeat、in-flight run 和 stale run 识别；
- 中断在 Implementing 时无法判断“尚未修改”还是“已经修改但结果未返回”；
- 模型自报 changed_files 尚未与真实 diff 核对；
- Evidence 只有 path/summary，缺少稳定位置、来源版本、风险和决策关联；
- 没有统一 Artifact manifest、内容哈希、不可变记录和敏感内容策略；
- JsonSessionStore 没有跨进程锁，未来钉钉/服务端入口会产生并发风险。

## 3. 任务卡中有价值的部分

1. **集中管理状态转换**：禁止业务代码随意设置状态，所有转换通过 StateMachine；
2. **Handler 分离执行阶段**：降低 AgentEngine 体积，让各阶段可测试、可替换；
3. **追加式 Event Store**：为审计、Debug、回放和恢复建立事实时间线；
4. **Decision Record**：记录模型为何提出某项方案及其证据；
5. **Artifact 系统**：保存方案、真实 diff、测试和最终报告；
6. **Recovery Manager**：识别异常退出和未完成 run，而不是静默重放；
7. **分阶段迭代**：避免一次性重写核心并破坏当前可用流程；
8. **以验收场景驱动**：审批恢复、失败处置、修改原因查询都是有效验收方向。

## 4. 需要修正的部分

### 4.1 三种状态概念必须分开

当前和未来应分别保留：

| 概念 | 回答的问题 | 示例 |
| --- | --- | --- |
| `TaskState` | 整个任务处于什么生命周期阶段 | INSPECTING、WAITING_APPROVAL |
| `AgentMode` | 本轮 Runtime 有什么权限 | INSPECT、IMPLEMENT、VERIFY |
| `DecisionKind` | 模型本轮希望宿主做什么 | NEEDS_INPUT、QUERY_REQUIRED、COMPLETED |

当前 `AgentStatus` 本质更接近 `DecisionKind`/Outcome，而不是完整任务状态。Phase 0 不应直接删除
它，应新增 `TaskState` 并提供旧会话迁移；稳定后再决定是否重命名。

### 4.2 不建议把 Analyze/Investigate/Plan 都做成持久状态

当前模型常在一次 Inspect turn 中完成分析、调查和方案生成，宿主无法从最终 JSON 可靠知道三个
阶段的精确边界。强行拆成三个 Handler 会增加模型调用、Token、延迟，并制造虚假的可观测性。

建议：

- 持久状态先使用 `INSPECTING`；
- `AnalysisStarted`、`InvestigationObserved`、`ProposalProduced` 可作为活动事件；
- 只有未来 Runtime 能提供真实流式阶段事件后，再评估是否升级为子状态。

### 4.3 原状态图缺少现有业务状态

必须补充：

- `WAITING_INPUT`：需求澄清；
- `QUERYING_DATA`：宿主执行只读数据库计划；
- `WAITING_MODIFY_APPROVAL`：修改审批；
- `WAITING_VERIFY_APPROVAL`：验证审批；
- `RECOVERY_REQUIRED`：崩溃后副作用状态不确定；
- `PAUSED`：只允许在安全边界由用户主动暂停；
- `CANCELLED`：用户明确放弃任务。

### 4.4 FAILED 不应既是终态又自动进入 REPAIRING

失败至少分为：

- Runtime/Provider 暂时失败；
- 权限或策略失败；
- 实施过程崩溃且副作用不确定；
- 验证失败，说明方案或实现需要调整；
- 不可恢复的终态失败。

建议：

- 验证失败：`VERIFYING -> REPLANNING -> WAITING_MODIFY_APPROVAL`；
- 实施中崩溃：`IMPLEMENTING -> RECOVERY_REQUIRED`；
- 明确不可恢复：`* -> FAILED`；
- 不允许 `FAILED -> REPAIRING -> EXECUTING` 无条件自动循环；
- 任何新的写入方案都必须重新获得 modify 审批，并限制修复轮数。

### 4.5 StateMachine 不能只是包装赋值

以下写法仍然不够：

```python
state_machine.transition(session, new_state)
```

真正的转换服务还必须：

- 校验 expected state 和 allowed transition；
- 校验 task version，拒绝并发旧命令；
- 生成带 reason/actor/causation 的事件；
- 在同一事务中追加事件并更新快照；
- 返回新快照，而不是由 Handler 继续修改 session；
- 对重复 command/event 使用幂等键。

### 4.6 Event Store 必须和状态快照保持一致

如果 session JSON 与 event JSONL 分开写，崩溃可能出现：

- 状态已变但事件没写；
- 事件已写但状态仍旧；
- 同一个事件被重复追加。

推荐使用标准库 SQLite 作为 Task Snapshot、Event、Decision 和 Artifact metadata 的同一物理
存储，通过单事务提交。对外仍保留 `TaskStore`、`EventStore` 等端口，避免领域层依赖 SQLite。
旧 JSON session 通过一次性导入或兼容读取迁移，不在首个版本立即删除。

### 4.7 不能根据模型自报生成 CodeModified/TestExecuted 事实事件

当前 Runtime 只返回最终结果。以下事件必须来自宿主可验证证据：

- `CodeModified`：真实 diff/文件快照检测到变化后生成；
- `TestExecuted`：Runtime 工具事件或宿主实际执行记录生成；
- `ArtifactRecorded`：ArtifactStore 成功写入并计算哈希后生成。

模型输出只能形成 `DecisionRecorded` 或 `ModelReportedChange`，不能伪装成实际执行审计。

### 4.8 自动恢复必须保守

启动扫描发现 `IMPLEMENTING + RuntimeStarted - RuntimeCompleted` 时，不能直接继续执行。应先：

1. 判断旧进程或 lease 是否已经失效；
2. 核对 workspace 是否仍是同一路径；
3. 比较任务开始/实施前基线与当前真实 diff；
4. 判断是否存在已发生但未确认的副作用；
5. 转为 `RECOVERY_REQUIRED`；
6. 向用户展示证据，并提供“只读检查后恢复、重新规划、放弃”选项。

只读 Inspect 在确认无副作用后可以自动恢复；Implement/Verify 默认需要恢复决策。

## 5. 推荐 TaskState

```text
CREATED
  ↓
INSPECTING
  ├─ WAITING_INPUT ───────→ INSPECTING
  ├─ QUERYING_DATA ───────→ INSPECTING
  ├─ WAITING_MODIFY_APPROVAL ─→ IMPLEMENTING
  └─ COMPLETED

IMPLEMENTING
  ├─ WAITING_VERIFY_APPROVAL ─→ VERIFYING
  ├─ COMPLETED
  └─ RECOVERY_REQUIRED

VERIFYING
  ├─ COMPLETED
  ├─ REPLANNING ─→ WAITING_MODIFY_APPROVAL
  └─ RECOVERY_REQUIRED

任意安全边界：PAUSED
明确放弃：CANCELLED
不可恢复：FAILED
```

`AgentMode` 继续独立存在：

- `INSPECTING`、`WAITING_*`、`QUERYING_DATA`、`REPLANNING` 使用 INSPECT 权限；
- `IMPLEMENTING` 使用 IMPLEMENT 权限；
- `VERIFYING` 使用 VERIFY 权限；
- 等待、暂停和终态不启动 Runtime。

## 6. 推荐核心对象

### 6.1 Command

所有外部动作先转换成命令：

- `CreateTask`
- `SubmitUserInput`
- `GrantApproval`
- `RejectApproval`
- `ResumeTask`
- `PauseTask`
- `CancelTask`

命令包含 `command_id`、`task_id`、`expected_version`、actor 和 timestamp，用于并发控制与幂等。

### 6.2 AgentStateMachine

职责：

- 定义允许的转换表；
- 校验当前状态、目标状态和触发命令/事件；
- 产生 `StateTransitioned`；
- 不执行模型、不写文件、不直接持久化。

### 6.3 StateHandler

建议接口：

```python
class StateHandler(Protocol):
    def handle(self, context: TaskContext, command: AgentCommand) -> HandlerResult: ...
```

`HandlerResult` 返回决定、候选事件、Artifact 草稿和目标状态。Handler 不直接修改 session，也不
直接写 Event Store。首版只需要：

- `InspectHandler`
- `ImplementHandler`
- `VerifyHandler`
- `RecoveryHandler`

等待态由命令处理器接收输入或审批，不需要为每个等待状态建立空 Handler。

### 6.4 AgentEvent

建议统一 envelope：

```text
event_id
task_id
sequence
schema_version
event_type
timestamp
actor
correlation_id
causation_id
command_id
from_state / to_state
reason
sanitized_payload
```

事件使用过去式命名，例如 `TaskCreated`、`RuntimeStarted`、`DecisionRecorded`、
`ApprovalRequested`、`ApprovalGranted`、`StateTransitioned`、`ArtifactRecorded`、
`RecoveryRequired`、`TaskCompleted`、`TaskFailed`。

### 6.5 DecisionRecord

建议字段：

```text
decision_id / task_id / event_id
decision_type / summary / reason
evidence_refs / alternatives
confidence / risk_level
model / runtime_session_id
created_at
```

Evidence 建议增加 path、symbol、line range、summary、content hash 和 git commit/baseline ID。
行号只能作为提示，文件内容变化后可能失效。Decision Record 要明确是“模型提出的理由”还是
“宿主验证的事实”。

### 6.6 ArtifactStore

建议目录：

```text
~/.autocoding-agent/tasks/<task-id>/artifacts/
├─ manifest.json
├─ analysis.json
├─ context-summary.json
├─ proposal.json
├─ baseline-status.json
├─ changes.patch
├─ test-results.json
├─ recovery-report.json
└─ final-report.md
```

要求：

- 每个 Artifact 有 ID、类型、schema version、SHA-256、创建事件和大小；
- 使用原子写入，不允许 Handler 随意覆盖；
- patch/diff 可能包含秘密，只保存在本机，不自动注入 Prompt 或上传；
- 目标工作区本来可能是 dirty，必须先记录 baseline，不能把用户旧改动算成 Agent 改动；
- Artifact metadata 进入 SQLite，正文按大小决定存文件或数据库；
- 最终报告引用 Artifact ID，不复制大量日志。

### 6.7 RecoveryManager

职责：

- 扫描非终态 Task 和过期 run lease；
- 校验 event sequence、snapshot version、workspace 和 runtime session；
- 生成恢复报告；
- 将不确定任务转为 `RECOVERY_REQUIRED`；
- 只执行用户选择后的恢复命令，不直接自动重放写操作。

## 7. 推荐存储模型

SQLite 第一版建议至少包含：

```text
tasks(
  task_id, state, version, workspace, runtime_session_id,
  active_run_id, updated_at, snapshot_json
)

events(
  event_id, task_id, sequence, type, timestamp,
  causation_id, correlation_id, command_id, payload_json
)

decisions(
  decision_id, task_id, event_id, type, summary,
  reason, confidence, risk_level, payload_json
)

artifacts(
  artifact_id, task_id, event_id, type, path,
  sha256, size, schema_version, created_at
)

runs(
  run_id, task_id, state, mode, started_at,
  heartbeat_at, completed_at, result_event_id
)
```

关键约束：

- `(task_id, sequence)` 唯一；
- `command_id` 唯一或在 task 内唯一；
- task version 使用 compare-and-swap；
- 状态快照和新事件同事务提交；
- Event payload 有版本和大小限制；
- 数据库启用 WAL，并设置合理 busy timeout；
- 所有对外错误和日志继续脱敏。

## 8. 迭代计划

### Phase 0：语义与兼容契约（已完成）

目标：在不改变现有行为前提下确定新架构语言。

内容：

- 新增 `TaskState`、`AgentCommand`、`TransitionRule`、`FailureClass`；
- 明确 AgentStatus/DecisionKind、AgentMode、TaskState 三层边界；
- 建立旧 AgentStatus 到 TaskState 的迁移映射；
- 定义状态转换表、终态、可恢复态和权限映射；
- 增加架构决策记录和状态图测试；
- 固化当前 73 项测试为迁移基线。

验收：转换表单元测试完整；旧 session 可以加载；用户可见行为不变。

### Phase 1：StateMachine 接管状态转换（已完成）

目标：消除 AgentEngine 中直接状态赋值。

内容：

- 新建 `core/state_machine/`；
- `AgentStateMachine` 集中校验转换；
- 增加 expected state/version；
- Engine 使用命令驱动转换；
- 先把事件继续写入现有 session，降低一次迁移风险；
- 为非法转换、重复审批、完成后继续和旧命令增加负向测试。

验收：核心业务代码不再直接修改 TaskState；全部旧流程测试通过。

### Phase 2：原子 Event Store 与 Session 迁移（已完成）

目标：建立可回放、可并发保护的事实记录。

内容：

- 定义 EventStore/TaskRepository/UnitOfWork 端口；
- 实现 SQLiteTaskStore；
- 状态快照和事件同事务提交；
- 增加 sequence、command id、task version；
- 提供 JSON session 导入和只读兼容；
- 实现 `replay(task_id)` 并核对重建状态；
- 增加崩溃注入、重复事件和并发旧版本测试。

验收：事件回放得到与快照相同状态；重复命令不重复执行；并发旧版本被拒绝。

### Phase 3：Handler、Decision 与 Artifact（已完成）

目标：拆分业务阶段并建立可解释审计。

内容：

- 提取 Inspect/Implement/Verify/Recovery Handler；
- Handler 返回 `HandlerResult`，不直接持久化；
- 增加 DecisionRecord 和 EvidenceRef；
- 建立 ArtifactStore、manifest、proposal、baseline 和 final report；
- Implement 前采集工作区 baseline，完成后采集真实 diff；
- Verify 结果写入结构化 Artifact；
- 提供 `explain_change(task_id, path)` 应用接口。

验收：可以从决定、证据和真实 diff 回答“为什么修改这个文件”；模型自报与真实变更分开。

### Phase 4：Runtime 生命周期事件（已完成，Agent SDK 暂缓）

目标：让 Event Store 记录真实运行活动，而不只记录最终模型结果。

内容：

- 扩展 Runtime port：run_id、事件 sink/iterator、interrupt；
- 捕获 init、assistant、ToolUse、ToolResult、result；
- 为 Read/Edit/Write/Bash 形成脱敏 Runtime 事件；
- 记录 run start/complete/failure、heartbeat 和 terminal reason；
- 评估 Claude Agent SDK adapter；本迭代根据已安装 Claude Code `2.1.237` 的真实能力，选择
  CLI `stream-json` 作为兼容现有 DeepSeek 配置的实现，SDK adapter 暂缓；
- UI 先显示阶段事件，再考虑流式文本。

验收：CodeModified/TestExecuted 事件能对应真实 ToolUse 或宿主 diff/命令证据；Runtime 可中断并
留下确定终局或 RecoveryRequired。

### Phase 5：Recovery Manager（已完成）

目标：安全恢复中断任务，不重复副作用。

内容：

- 启动时扫描非终态 task 和 stale run；
- 增加 run lease/heartbeat；
- 生成 workspace/diff/runtime 恢复报告；
- 只读任务支持安全自动恢复；
- 写入或验证任务进入 `RECOVERY_REQUIRED`；
- UI 提供只读检查后继续、重新规划、暂停、取消；
- 验证失败进入 Replanning，设置最大修复轮数并重新审批。

验收：在 Runtime 启动前、编辑中、编辑后未返回、验证中四个故障点注入崩溃，均不会自动重复
写入，且可以从事件和 Artifact 解释恢复选择。

### Phase 6：产品接入与稳定化（开发流程已完成）

目标：把新 Runtime 能力完整暴露给现有入口。

内容：

- 桌面端显示任务状态时间线、恢复卡和 Decision 详情；
- CLI 增加 events、artifacts、explain、resume、pause、cancel；
- 异常流程继续保留独立领域状态机；复用通用 Event/TaskStore 调整为后续独立迭代；
- 日志增加 task/run/event ID 关联；
- 迁移文档、数据备份和回退工具；
- 性能、事件体积、脱敏、故障注入和 live 回归测试。

验收：旧任务可查看，新任务使用新状态机；桌面、CLI 输出一致；回退不破坏已有 JSON 数据。

## 9. 修订后的验收标准

### 场景 A：审批等待后重启

- 任务进入 `WAITING_MODIFY_APPROVAL`；
- 关闭并重新启动客户端；
- Task snapshot 和 Event replay 状态一致；
- UI 恢复同一 proposal、session ID 和审批；
- 批准后只执行一次。

### 场景 B：实施中崩溃

- 存在 RuntimeStarted，无 RuntimeCompleted；
- 启动扫描把任务转为 `RECOVERY_REQUIRED`；
- 系统生成 baseline/current diff 恢复报告；
- 未经用户恢复决策不自动重新执行 Edit/Write；
- 事件时间线能解释状态为何不确定。

### 场景 C：验证失败

- 记录真实 TestExecuted/TestResult Artifact；
- 任务从 VERIFYING 进入 REPLANNING；
- Decision Record 解释失败证据和新方案；
- 新修改必须再次等待 modify 审批；
- 达到最大修复轮数后转为 FAILED，不无限循环。

### 场景 D：解释文件修改

调用 `explain_change(task_id, "path/to/file")` 返回：

- 关联 proposal 和 decision；
- 模型理由及置信度；
- 证据路径/符号/基线；
- 实际 diff Artifact；
- 风险和验证结果；
- 所有来源 event ID。

### 场景 E：事件一致性

- Event sequence 连续且不可重复；
- replay 结果与 snapshot 一致；
- 相同 command ID 重试不会重复执行；
- 旧 expected version 被拒绝；
- 状态与事件在故障注入下不出现半提交。

### 场景 F：安全与兼容

- 旧 JSON session 可以加载或迁移；
- Event、Decision、Artifact、日志不含 API Key、数据库密码和原始业务行；
- 历史知识和 Artifact 不扩大 Runtime 权限；
- 非 Git 工作区和 dirty Git 工作区有明确、经过测试的 baseline 策略。

## 10. 暂不纳入首轮范围

- 多 Agent 调度、任务 DAG 和并行写入；
- 分布式 Event Bus；
- 云端 Artifact Storage；
- 数据库写入和异常自动修复；
- 完整 CQRS/Event Sourcing 重构；
- 为了展示进度而强制把一次 Inspect 拆成多次模型调用；
- 无人工确认的自动 repair loop。

本任务完成后会具备未来多 Agent 所需的事件、版本、命令、Artifact 和 ownership 基础，但不代表
多 Agent 本身已经实现。

## 11. 主要风险与回退

| 风险 | 控制措施 |
| --- | --- |
| 状态迁移破坏旧 UI/CLI | 保留 AgentStatus 兼容层，逐阶段迁移 |
| Event 与 Snapshot 不一致 | SQLite 单事务、sequence、replay 检查 |
| 自动恢复重复写入 | 写状态统一转 RecoveryRequired，禁止自动重放 |
| Artifact 泄密 | 本地存储、大小限制、清单、脱敏与不自动上传 |
| Handler 过度拆分 | 首版仅四个有真实行为差异的 Handler |
| Runtime 升级影响 DeepSeek | 保留当前 CLI adapter 和 live 兼容测试 |
| 数据迁移失败 | 迁移前备份、幂等导入、旧 JSON 只读兼容 |
| 范围过大 | 每个 Phase 独立提交、完整测试、可单独回退 |

## 12. 相关路径

- `src/autocoding_agent/core/models.py`
- `src/autocoding_agent/core/engine.py`
- `src/autocoding_agent/core/state_machine/`
- `src/autocoding_agent/core/handlers/`
- `src/autocoding_agent/core/audit/`
- `src/autocoding_agent/core/artifacts/`
- `src/autocoding_agent/core/runtime/`
- `src/autocoding_agent/core/recovery/`
- `src/autocoding_agent/core/policies.py`
- `src/autocoding_agent/adapters/claude_code.py`
- `src/autocoding_agent/adapters/sqlite_task_store.py`
- `src/autocoding_agent/adapters/task_artifact_store.py`
- `src/autocoding_agent/adapters/workspace_snapshot.py`
- `src/autocoding_agent/ports/runtime.py`
- `src/autocoding_agent/ports/session_store.py`
- `src/autocoding_agent/interfaces/desktop_ui.py`
- `tests/test_agent_flows.py`
- `tests/test_claude_runtime.py`
- `docs/ARCHITECTURE.md`
- `docs/INTERFACES.md`
- `docs/PROJECT_EXPERIENCE.md`

## 13. 尝试与验证记录

| 时间 | 操作 | 证据 | 结果 | 下一步 |
| --- | --- | --- | --- | --- |
| 2026-08-25 | 对照当前 Engine、Models、Runtime、Store 和测试评估原任务卡 | 当前代码中已有 AgentEvent、精确 resume、原子 JSON，但状态直接赋值且无独立 Event Store | 证据充分，形成修订架构与六阶段计划 | 等待用户确认 Phase 0 范围 |
| 2026-08-25 | 用户确认继续实施，并要求同步项目开发与工程经验文档 | 用户明确授权继续任务卡 | 状态改为 in_progress，开始 Phase 0/Phase 1 | 实现状态模型、StateMachine、Engine 接入和迁移测试 |
| 2026-08-25 | 完成 Phase 0/1 实现与文档同步 | 30 项状态机/开发流程专项测试；完整测试 86 passed；Ruff 通过；仅 StateMachine 写 task_state | Phase 0/1 已确认完成 | 开始 Phase 2 SQLite Task/Event Store |
| 2026-08-25 | 完成 Phase 2 SQLite Task/Event Store | 事件 sequence、不可变、replay、旧 revision 拒绝、事务故障回滚、旧 JSON 导入专项测试；完整测试 93 passed；Ruff 通过 | Phase 2 已确认完成 | 开始 Phase 3 Handler、Decision 与 Artifact |
| 2026-08-25 | 完成 Phase 3 Handler/Decision/Artifact | clean、dirty、non-Git baseline；SHA-256/脱敏/原子产物；Decision explanation 专项测试 | 模型声明与宿主事实完成分离 | 开始 Runtime 生命周期观察 |
| 2026-08-25 | 核对本机 Claude Code 能力并完成 Phase 4 | 本机 `D:\claude\claude.ps1` 版本 2.1.237；help 确认 stream-json/hook events；Runtime stream、interrupt 与 ToolResult 测试 | 采用 CLI stream-json，暂缓 SDK 迁移 | 开始 Recovery Manager |
| 2026-08-25 | 完成 Phase 5/6 开发流程接入 | 孤儿运行、实施中断不重试、恢复动作、重规划上限、命令幂等、CLI/UI 恢复卡测试 | 开发 Runtime 验收完成 | 同步文档、版本并发布本次迭代 |
| 2026-08-25 | 完整回归与静态检查 | `python -m pytest -q`：113 passed；`python -m ruff check src tests`：通过；`git diff --check`：通过（仅换行提示） | 任务完成，无阻塞 | 形成 0.4.0 单一迭代提交并推送 GitHub |

## 14. 阻塞与待确认

- 阻塞：无。
- 已验证：开发流程状态机、事件、运行记录、决策、产物、恢复、命令幂等、CLI 和桌面入口均有
  确定性测试覆盖；真实 Claude CLI 版本和协议能力已核对。
- 未执行：需要消耗外部模型额度的 live 业务任务；本次不把单元/协议验证描述成真实模型回归。
- 后续独立任务：异常流程复用通用 Event/Recovery 基础设施、Windows Job Object、流式 Token UI、
  Engineering Experience Knowledge/RAG 和多 Agent 编排。

## 15. 交付物

- 本任务卡：`docs/tasks/ACE-RUNTIME-001-agent-engine-runtime-upgrade.md`
- 用户原始任务目标的架构评估；
- 修订状态模型；
- 六阶段实施与验收记录；
- `core/state_machine/`、`core/handlers/`、`core/audit/`、`core/artifacts/`、`core/runtime/`、
  `core/recovery/`；
- SQLite Task/Event/Decision/Artifact/Run/Command Store；
- CLI 生命周期查询与恢复命令、桌面 Recovery 卡；
- 同步后的架构、接口和项目开发与工程经验文档。
