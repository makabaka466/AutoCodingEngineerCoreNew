# AutoCodingEngineerCoreNew 项目开发与工程经验

> 文档基线：2026-08-31，项目版本 `0.7.3`。本文以当前代码为准，并明确区分“已实现”、
> “当前限制”和“后续规划”。当前 Agent 已具备状态机、追加事件、运行记录、决策审计、
> 任务产物、保守恢复，以及按会话持续沉淀开发/异常能力知识的 Runtime 内核。

## 1. 文档目的

本文不是简单的 README，也不是逐个接口的参考手册。它用于沉淀 AutoCodingEngineerCoreNew
从需求形成、架构重构到当前实现过程中的完整工程经验，重点回答：

- 为什么要做这个项目，它解决什么问题；
- 产品真正的业务对象是什么，而不是什么；
- 为什么采用当前架构和安全边界；
- 两套 Agent 流程如何运转；
- Claude Code、模型、数据库、会话、能力文档和 UI 如何组合；
- 开发过程中遇到过哪些真实问题，如何定位和解决；
- 哪些方案经过权衡后被采用，哪些方案被暂缓或放弃；
- 当前系统还有哪些限制，下一阶段应如何建设工程经验知识体系。

本文面向后续维护者、架构设计者和继续开发本项目的 Agent。具体字段和 API 应同时参考
`docs/INTERFACES.md`，当前架构细节应同时参考 `docs/ARCHITECTURE.md`。

---

## 2. 项目背景与演进

### 2.1 初始问题

项目最初希望提供一个能够帮助用户完成代码开发的工具。早期思路容易落入“平台化”方向：
自己建设需求分析、任务拆分、文件扫描、规则判断、执行调度和页面交互。实践后发现，过多的
宿主规则会产生三个问题：

1. 宿主用文件名、关键词或固定步骤替代模型理解，限制了 Claude Code 的代码调查能力；
2. 为了获得上下文而扫描整个项目，消耗大量时间与 Token，且经常读取无关代码；
3. 平台层逐渐变成主要复杂度，而真正有价值的 Agent 工程能力反而被规则包围。

因此项目进行了方向调整：平台只作为交互媒介，真正产品是一个平台无关的 Agent 内核。
模型负责理解和语义决策，Python 宿主只负责不能交给模型自行决定的硬边界。

### 2.2 核心定位变化

项目经历了以下认知变化：

```text
早期：开发平台 + 大量固定流程 + 全项目分析
  ↓
中期：把用户问题交给 Claude Code，但仍由宿主判断部分需求规则
  ↓
当前：模型负责需求理解、文件选择、调查、诊断和方案；宿主负责权限、状态、数据和持久化
```

当前产品定位是：

> 一个以 Claude Code 为执行运行时、以模型语义能力为核心、由宿主提供确定性安全边界的
> 平台无关工程 Agent 内核。

桌面客户端、CLI、备用 Web UI，以及未来的钉钉机器人，都只是这个内核的输入输出适配器。

### 2.3 当前业务场景

系统当前服务两类业务：

1. **软件开发**：理解需求、调查代码、提出修改方案、等待授权、实施修改、执行验证并沉淀能力；
2. **异常处理**：根据问题和页面线索定位代码，按需查询 SQL Server 业务数据，输出诊断证据和
   建议，但不修改代码、不写数据库、不自动处理生产异常。

指纹 MES 是当前已配置的一个知识项目，也是两套流程的重要验证场景，但它不是
AutoCodingEngineerCoreNew 的硬编码业务。MES 的目录结构、页面定位 SQL 和开发约束保存在
项目知识 Markdown 中，由用户在对话页选择“项目”后按需注入。

### 2.4 当前阶段

当前版本已经具备：

- 原生桌面客户端及双击启动；
- 开发与异常诊断两套独立流程；
- Claude Code 与 Anthropic 兼容模型接入；
- 模型配置、Claude Code 检测和 API Key 配置页面；
- 两套流程共用的 SQL Server 只读连接；
- 项目知识二级分支和 Markdown 编辑；
- 多轮会话、精确恢复、修改与验证审批；
- 独立 TaskState、命令 envelope、集中转换规则和带原因的状态转换事件；
- SQLite 原子 Task/Event Store、命令幂等、Runtime 活动时间线与运行租约；
- 真实工作区基线/差异 Artifact、Decision Record 和修改原因查询；
- 暂停、取消、启动扫描、保守恢复与有上限的验证失败重规划；
- 结构化模型输出和宿主二次校验；
- 开发、异常各自独立的 Capability 文档；
- 人工选择的 Markdown 分块、FTS5 + Voyage/模拟向量混合检索、分领域 Agent 注入与检索审计；
- 对话优先、图片补充、标题/路径联合判断和有界页面映射的异常调查规则；
- 双流程瞬时进度投影，以及可选 Hermes 工程经验 Skill 的只读咨询、审计和失败降级；
- 本地脱敏日志、确定性测试和版本回退规则。

尚未接入外部向量数据库、跨工作区工程经验治理、自动异常修复、钉钉接入、流式 Token 展示和
多 Agent 编排。开发 Runtime 已支持进程级 interrupt，但还没有 Windows Job Object 级进程树治理。

---

## 3. 业务理解

### 3.1 用户真正需要的不是“自动写代码”

用户在真实工程环境中需要的是一条可信的闭环：

```text
说清问题
  → 找到正确位置
  → 阅读必要代码和数据
  → 解释当前状态
  → 给出可审查方案
  → 获得授权
  → 实施最小完整修改
  → 验证结果
  → 保存可复用经验
```

代码生成只是中间步骤。定位是否准确、方案是否可理解、权限是否受控、结果是否经过验证，
决定了 Agent 能否进入真实项目。

### 3.2 需求澄清原则

需求是否清楚属于语义判断，不适合由宿主通过“是否包含文件名”“是否包含路径”等规则判定。
当前做法是：

- 模型判断现有信息是否足够；
- 不足时每轮只询问一个最关键、信息价值最高的问题；
- 用户给出文件名、路径、页面或业务线索后，模型读取对应目标并追踪必要关联代码；
- 找不到目标、结果不唯一或仍缺关键上下文时继续澄清；
- 不通过全仓扫描弥补缺失意图。

这使 Agent 保留理解灵活性，同时控制 Token 和调查范围。

### 3.3 开发流程业务规则

开发流程的业务状态如下：

```text
用户需求
  ↓
Inspect：模型只读调查
  ├─ 信息不足 → needs_input → 用户回答 → 恢复同一会话
  ├─ 需要业务数据 → query_required → 宿主执行只读 SQL → 恢复同一会话
  ├─ 只需结论 → completed
  └─ 需要修改 → approval_required(modify + 结构化方案)
                         ↓ 用户批准
                    Implement：允许 Edit/Write
                         ├─ 不需命令 → completed
                         └─ 需要验证 → approval_required(verify)
                                               ↓ 用户批准
                                          Verify：仅允许受控命令
                                               ↓
                                           completed
```

修改前必须呈现：

- 当前是什么；
- 要改成什么；
- 涉及哪些文件或区域；
- 预期结果；
- 影响和验证方法；
- 合适时提供界面线框、伪代码、接口示例或行为前后对比。

普通对话回复在存在审批时会被视为“调整要求”，并清除旧审批，防止用户修改需求后仍执行旧方案。

### 3.4 异常诊断业务规则

异常流程的目标不是立即修复，而是形成可靠诊断：

```text
问题描述 + 页面线索
  ↓
定位页面、路由、窗体或模块
  ↓
追踪最小代码链路
  ↓
按需提出最小只读 SQL
  ↓
宿主校验、执行、限行、脱敏
  ↓
模型结合代码和数据给出诊断、证据、置信度和建议
```

当前异常流程明确禁止：

- 修改目标仓库；
- 执行 Shell 命令；
- 写数据库；
- 调用真实外部修复接口；
- 在证据不足时声称已找到唯一根因；
- 把“适合自动化”解释为“现在已经可以自动执行”。

这种边界为后续钉钉接入保留了安全前提：钉钉可以创建、继续和展示异常任务，但不会因为入口
自动化而绕过诊断边界。

### 3.5 项目知识的业务含义

界面中的“开发”和“异常处理”是一级流程；每个流程下面可以有多个二级项目。一个项目对应
一份同名 Markdown：

```text
knowledge/
├─ development/<项目>/<项目>.md
└─ incident/<项目>/<项目>.md
```

项目知识保存稳定、跨页面的当前项目事实，例如技术栈、目录结构、常用调用链、页面定位方式和
开发约束。单次任务状态、临时异常数据和某个页面的短期结论不应持续追加到项目知识中。

当前“生物”项目中，页面名称可以通过参数化只读 SQL 查询 `Menu` 表，使用返回的 `URL` 作为
相对代码位置线索。该规则属于用户选择的项目知识，不是宿主代码中的通用硬规则。

### 3.6 五类知识的边界

| 类型 | 作用 | 当前存储 |
| --- | --- | --- |
| Skills | Agent 应该怎样工作 | `src/autocoding_agent/skills/` |
| Project Knowledge | 当前项目是什么、有哪些稳定约束 | `knowledge/<flow>/<project>/` |
| Session Memory | 当前任务进度、消息、审批和查询审计 | `~/.autocoding-agent/sessions`、`incidents` |
| Capability | 某工作区已完成任务产生的可复用能力 | `~/.autocoding-agent/workspaces/...` |
| Engineering Experience | 跨项目设计经验、问题解决经验和失败教训 | 尚未实现，下一阶段建设 |

---

## 4. 总体设计思路

### 4.1 模型负责语义，宿主负责边界

这是项目最重要的设计原则。

交给模型的内容：

- 理解用户意图；
- 判断是否需要追问；
- 选择需要读取的文件；
- 判断代码关系；
- 定位页面和调用链；
- 提出数据库查询计划；
- 诊断根因；
- 制定修改与验证方案；
- 判断任务何时可以如实完成；
- 总结能力草稿。

由宿主控制的内容：

- 工作区是否存在；
- 当前模式能看到哪些工具；
- 是否已经获得修改或验证授权；
- SQL 是否只读、参数是否绑定、结果是否限行和脱敏；
- 模型输出是否符合结构化契约；
- 路径是否越界；
- 会话、日志、配置和能力文档如何持久化；
- 密钥保存位置；
- 失败是否可追踪。

宿主不判断“哪个文件语义上相关”，但必须判断“这个路径是否越过安全边界”。两类判断不能混淆。

### 4.2 不重写 Claude Code Agent Loop

项目选择复用 Claude Code 的代码搜索、文件读取、编辑和命令执行能力，而不是自行重写工具调用
循环。Python Runtime 负责构造安全参数、传入系统提示词和 JSON Schema，再解析最终结构化结果。

这样做的收益：

- 保留 Claude Code 成熟的代码调查能力；
- 避免维护自定义工具协议和复杂 Agent Loop；
- 可以使用 Claude Code 的精确 session resume；
- 模型可以自然选择 Read、Glob、Grep、Edit、Write、Bash。

代价是仍需维护 CLI 协议兼容。当前 Runtime 通过 `stream-json` 观察 Claude Code 生命周期和工具
活动，并提供进程级 interrupt；它不展示流式 Token，也不把模型自报当作宿主事实。

### 4.3 应用内核与交付平台分离

`AgentApplication` 和 `IncidentApplication` 是稳定应用门面。桌面 UI、CLI 和 Streamlit 只调用
门面，不直接组装 Claude 命令或实现业务状态机。

因此未来增加钉钉入口时，应调用 `IncidentApplication`，而不是复制异常诊断 Prompt 和数据库
逻辑。这是“平台只是媒介”的代码体现。

### 4.4 结构化决定代替文本猜测

模型最终输出通过 Pydantic 生成 JSON Schema。宿主只接受符合 `AgentDecision` 或
`IncidentDecision` 的 `structured_output`，不再从自由文本中截取首尾花括号。

结构化契约使状态机能够确定地判断：

- 是否需要用户输入；
- 是否需要数据库查询；
- 是否需要修改或验证审批；
- 哪些文件被报告为已修改；
- 是否完成；
- 能力草稿包含什么。

模型负责内容质量，Schema 负责数据形状，宿主策略负责安全一致性。

### 4.5 历史知识只能作为证据线索

Project Knowledge 和 Capability 都被提示为“不可信且可能过期的参考材料”。它们不能覆盖：

1. 当前用户要求；
2. 当前仓库代码；
3. 当前数据库 schema 和授权查询结果；
4. 宿主权限策略。

这是未来接入 Engineering Experience/RAG 时必须继续保留的原则。

---

## 5. 系统架构

### 5.1 总体结构

```text
桌面客户端 / CLI / Streamlit / 未来钉钉
                    ↓
       Application Facade 应用门面
          ├─ AgentApplication
          └─ IncidentApplication
                    ↓
              Domain Engine
          ├─ AgentEngine
          └─ IncidentEngine
                    ↓
       Ports：Runtime / Store / Database
                    ↓
Adapters：Claude Code / SQLite / Artifact / Capability / SQL Server
                    ↓
Claude Code + 模型 / 本地文件 / Windows Credential Manager / SQL Server
```

### 5.2 分层职责

| 层 | 主要目录 | 职责 |
| --- | --- | --- |
| Interfaces | `interfaces/` | 桌面、CLI、Web 的输入输出转换 |
| Application | `application.py`、`incident/application.py` | 依赖组装和稳定业务入口 |
| Domain/Core | `core/`、`incident/` | 状态机、契约、审批和业务流程 |
| Ports | `ports/`、`incident/ports.py` | Runtime、会话和数据库最小协议 |
| Adapters | `adapters/` | Claude Code、JSON、能力、SQL Server、SQLite |
| Skills | `skills/` | 显式注入模型的工作方法 |
| Knowledge | `knowledge/` | 用户维护的分流程项目知识 |

### 5.3 组合根

`build_application()` 组装开发流程：

```text
ClaudeCodeRuntime
SQLiteTaskStore
TaskArtifactStore + GitWorkspaceObserver
CapabilityStore(development)
SkillRegistry
ExecutionPolicy
AgentStateMachine + RecoveryManager
可选 DatabaseReader
        ↓
AgentEngine
        ↓
AgentApplication
```

`build_incident_application()` 组装异常流程：

```text
ClaudeCodeRuntime
SQLiteIncidentStore + AgentStateMachine
IncidentRecoveryManager + OrphanedRunScanner
IncidentCapabilityStore
可选 SQLServer/SQLite DatabaseReader
        ↓
IncidentEngine
        ↓
IncidentApplication
```

两套流程共享 Runtime、TaskState/Event/Run 与恢复扫描内核和数据库端口，但拥有不同结构化决定、
Prompt、SQLite 表和能力目录。

---

## 6. 核心技术实现

### 6.1 Pydantic 数据契约

项目使用 Pydantic 2 定义稳定契约，主要包括：

- `AgentSession`：开发任务的完整持久化状态；
- `AgentDecision`：开发模型每轮唯一合法输出；
- `ChangeProposal`：修改前的结构化方案；
- `IncidentSession`：异常任务状态；
- `IncidentDecision`：页面、查询、诊断和建议；
- `DataQuery`：两套流程共用的最小只读查询计划；
- `QueryResult`：本轮返回模型的脱敏结果；
- `QueryObservation`：可以长期保存但不含原始业务行的审计摘要。

模型校验器保证状态与载荷一致。例如：

- `approval_required` 必须携带审批对象；
- 非审批状态不能夹带审批；
- `query_required` 必须包含查询；
- 异常诊断完成时必须包含已定位页面和诊断结论；
- 新的 modify 审批必须在 Engine 层额外验证完整方案。

### 6.2 任务生命周期 StateMachine

状态机升级后，开发流程不再把一个 `status` 同时用作模型结果、权限和任务生命周期。系统明确
分成三层：

| 层 | 职责 |
| --- | --- |
| `TaskState` | 当前任务处于 inspecting、waiting、implementing、verifying 或终态 |
| `AgentStatus` | 模型本轮返回 needs_input、query_required、approval_required、completed、failed |
| `AgentMode` | Claude Code 本轮获得 INSPECT、IMPLEMENT 或 VERIFY 工具权限 |

`core/state_machine/` 定义：

- `TaskState`：14 个当前及预留生命周期状态；
- `AgentCommand`/`AgentCommandType`：命令 ID、task ID、expected version、actor 和 payload；
- `TransitionRule`：每个来源状态允许进入的目标集合；
- `FailureClass`：为后续恢复策略预留的失败类别；
- `AgentStateMachine`：校验转换、拒绝旧版本、增加 task version 并生成转换事件。

AgentEngine 的 start、send、approve、reject 都先构造命令。业务代码不直接写
`session.task_state`；真正转换统一经过 StateMachine，并记录 from、to、reason、actor、version 和
command ID。转换到相同状态是幂等 no-op，非法转换和 stale expected version 会被明确拒绝。

为了兼容已经保存的 session，缺少 TaskState 的旧 JSON 会根据 `AgentStatus` 和 pending approval
推导状态，version 从 0 开始。当前 `AgentStatus` 字段仍由 Engine 保存，因为它表示最后模型决定，
不是生命周期状态。

Phase 2 已将开发任务切换到 SQLiteTaskStore：Task snapshot 和新 Event 在同一事务提交，事件按
task 获得连续 sequence，snapshot 使用 revision 拒绝并发旧保存，已追加事件不能修改，并支持
按状态事件回放生命周期。旧 JsonSessionStore 文件会在启动时幂等导入，并为缺少生命周期事件
的历史任务生成 migration 事件。`replanning`、`paused`、`recovery_required`、`cancelled` 均已
进入公共操作；重复 command ID 由持久化 receipt 返回已有结果，不会再次调用 Runtime。

### 6.3 Claude Code Runtime

开发流程的可观测 Runtime 使用 `subprocess.Popen()` 调用真实 `claude.exe`，逐行读取
`stream-json`；异常流程和兼容调用仍可使用最终 JSON 路径。主要参数包括：

```text
-p
--bare
--no-chrome
--strict-mcp-config
--mcp-config {"mcpServers":{}}
--setting-sources ""
--output-format stream-json
--include-hook-events
--model <configured model>
--permission-mode dontAsk
--tools <mode-specific tools>
--allowedTools <host-approved tools>
--append-system-prompt <skills + boundaries>
--json-schema <Pydantic schema>
--session-id <new id> 或 --resume <exact runtime id>
```

关键设计：

- `--bare`、空 setting sources 和严格空 MCP 隔离目标仓库及用户全局设置，防止其扩展权限；
- `--no-chrome` 避免加载浏览器集成；
- Runtime 逐行解析 system、assistant、ToolUse、ToolResult 和 result envelope；
- `structured_output` 再经过一次 Pydantic 校验；
- 必须获得可恢复的 Claude session ID，否则本轮视为失败；
- Runtime 记录 Token、成本、耗时和 turn 数；
- 每个 run 持久化 owner、PID、heartbeat、终态原因和脱敏活动；
- `interrupt(run_id)` 只终止已登记的本地进程，留下 interrupted 或 recovery_required 事实；
- Provider 错误和 stderr 在显示及日志前脱敏。

### 6.4 精确会话恢复

应用 session ID 与 Claude session ID 分开建模，但首轮会使用应用 UUID 预分配 Claude session。
首次启动前先把该 ID 写入应用会话，随后才调用模型。

这样即使首轮超时或模型进程异常，也不会因为应用不知道旧 ID 而静默创建新会话、重复执行一轮
可能已经发生副作用的任务。后续轮次始终使用精确 `--resume <runtime_session_id>`，不使用按目录
推断的 continue，防止同一工作区多个任务串线。

### 6.5 权限档位

| 模式 | 可见工具 | 自动允许范围 |
| --- | --- | --- |
| Inspect | Read、Glob、Grep | 全部只读工具 |
| Implement | Read、Glob、Grep、Edit、Write | 已批准任务中的文件读写 |
| Verify | Read、Glob、Grep、Bash | 仅预定义测试、构建、静态检查和 Git 只读命令 |

Verify 白名单包括 pytest、ruff、npm test/lint/typecheck、dotnet build/test、go test、cargo test、
git status 和 git diff。当前没有把任意 Bash 暴露给模型。

Capability 目录位于目标工作区之外。它只在 Inspect 模式通过 `--add-dir` 挂载；Implement 和
Verify 不再挂载，防止已经获得仓库写权限的模型修改长期能力记忆。

### 6.6 修改方案与审批

修改审批要求 `ChangeProposal` 至少包含一项变更，每项说明：

- `path` 或影响区域；
- 当前状态；
- 修改后的状态；
- 总体目标和预期结果；
- 可选影响、验证计划和 Markdown 预览。

用户批准后，Engine 会把已审查方案重新写入继续指令，要求模型只执行该范围。历史会话中缺少
proposal 的旧审批仍可以加载，但不能直接批准执行，必须让模型重新调查和生成方案。

### 6.7 宿主二次校验

Schema 合法不代表行为合法。Engine 还会检查：

- Inspect 模式不能报告 `changed_files`；
- Implement/Verify 期间不能请求数据库查询；
- modify 审批不能缺少方案；
- 证据路径、方案路径和变更路径不能是绝对路径；
- Windows drive/root、Unix root 和 `..` 路径均被拒绝。

这些检查用于守住边界。Implement 前后还会由宿主采集 Git status 和 staged/unstaged diff，分别
形成 baseline 与 current Artifact；只有真实哈希发生变化才记录 `code_modified`。模型自报的
`changed_files`、`test_summary` 和宿主观察事实分开保存，不能互相冒充。非 Git 工作区会明确
记录“无法生成真实 patch”，不会伪造差异。

### 6.8 SQL Server 只读访问

两套流程共用 `DatabaseReader` 端口和一份 SQL Server 配置。模型不直接获得数据库连接，而是
返回 `DataQuery`，由宿主执行。

当前防线包括：

1. 只接受以 `SELECT` 或 `WITH` 开头的语句；
2. 拒绝分号、多语句、SQL 注释和空字节；
3. 去除字符串和引号标识符后，再拦截 INSERT、UPDATE、DELETE、MERGE、EXEC、DDL、
   BACKUP、DBCC、WAITFOR、KILL 等关键词；
4. 命名参数由宿主转换为 `?` 参数并单独绑定，不拼接用户输入；
5. 连接串使用 `ApplicationIntent=ReadOnly`；
6. 单条结果受行数限制，超长值截断，二进制只返回长度；
7. password、token、secret、credential、cookie、session 等敏感列整列脱敏；
8. 默认最多两轮查询、每条最多 100 行、查询超时 60 秒；
9. 原始业务行只发给当前模型轮次，不写入应用会话和能力文档；
10. 会话绑定启动时的安全数据库引用，配置变更只影响新任务。

`ApplicationIntent=ReadOnly` 和 SQL 文本校验不能替代数据库权限。生产环境仍应使用只有 SELECT
权限的专用账号，形成数据库侧最终边界。

### 6.9 SQL Server 配置与秘密管理

非秘密配置写入：

```text
~/.autocoding-agent/database/sqlserver.json
```

SQL Server 密码通过 `keyring` 保存到 Windows Credential Manager，不进入 JSON。保存新配置时
先记录旧密码；若配置文件原子替换失败，会尽力恢复旧凭据，避免出现“密码已换但配置未换”的
半成功状态。

配置字段拒绝分号、花括号、空字节和换行，避免把单个 UI 字段注入为额外连接串属性。驱动列表
从 `pyodbc.drivers()` 动态读取，并优先显示版本较新的 Microsoft ODBC Driver。

### 6.10 模型与 Claude Code 配置

客户端启动时通过真实可执行文件的 `--version` 检查 Claude Code。Windows 下不依赖可能受
PowerShell 执行策略影响的 `claude.ps1`，优先使用真实 `.exe`/`.com`。

配置页面维护：

- Claude Code 可执行文件；
- Anthropic 兼容 API 地址；
- 模型名称；
- API Key。

非密钥信息和 API Key 当前保存到 Windows 当前用户环境变量，API Key 不回填到输入框，也不
写入项目 `.env`、会话或日志。保存后清除 Settings 缓存，使新任务使用最新配置。

### 6.11 桌面客户端

桌面端选择标准库 Tkinter/ttk，而不是把 Streamlit 嵌入 WebView。主要原因：

- 当前 Python 已包含 Tk 8.6，不增加大型 GUI 依赖；
- 无需启动本地 HTTP 服务；
- 不依赖 WebView2；
- 避免服务进程和窗口双重生命周期；
- 更容易通过 `pythonw.exe` 实现双击无控制台启动。

Claude Runtime 对应用门面仍表现为同步调用。为避免界面冻结，桌面端使用单一后台线程执行
start/send/approve/reject/resume，通过结果队列和 `root.after()` 回到 Tk 主线程更新控件。忙碌时
禁用会触发新轮次的按钮，防止同一 session 并发 resume。

UI 提供：

- 开发/异常处理胶囊式流程选择，当前项以颜色标识；
- 工作区输入与选择；
- “项目”知识分支选择及相对 MD 路径；
- 页面线索输入；
- 会话列表；
- 方案预览和批准/拒绝；
- RecoveryRequired 的只读检查、重新规划和取消恢复卡；
- 模型、SQL Server、MD 知识统一配置；
- 本地日志入口。

### 6.12 Windows 子进程与隐藏窗口

Claude Code 检测和每轮问答都使用 `STARTF_USESHOWWINDOW + SW_HIDE + CREATE_NO_WINDOW`，避免
每次调用弹出控制台。根目录 `start.cmd` 调用 `start.ps1`，脚本检查 Python 3.12、依赖和
Tkinter 后，默认使用 `pythonw.exe` 启动原生客户端并退出启动控制台。

备用 Web UI 通过 `start.cmd -Web` 显式启动，不再作为默认入口。

### 6.13 会话持久化

开发任务和异常任务都使用 SQLite 事务存储：

```text
~/.autocoding-agent/
├─ runtime/agent-runtime.db
├─ sessions/<uuid>.json      # 旧开发会话导入来源
└─ incidents/<uuid>.json     # 旧异常会话导入来源
```

开发和异常 Task/Event 表都只接受 UUID。SQLite 使用 WAL、foreign key、busy timeout 和
`BEGIN IMMEDIATE`，在一个事务中追加事件并更新快照；内存 event sequence 或 revision 在事务
失败时恢复。异常使用独立 `incident_*` 表；旧 JSON 幂等导入但不覆盖或删除。

SQLiteTaskStore 与 SQLiteIncidentStore 都通过 snapshot revision 拒绝并发旧更新；桌面端仍通过
忙碌状态规避本窗口内并发。每个 Runtime run 保存 owner ID、进程 PID 和 heartbeat。两套恢复
管理器共享 `OrphanedRunScanner`：开发只读 Inspect 和异常诊断转为 paused，开发
Implement/Verify 转入 recovery_required，均不自动重放旧 Runtime。

### 6.14 Capability 与 Project Knowledge

已完成的开发任务生成开发 Capability，已完成的异常任务生成异常 Capability。两者按工作区和
流程隔离：

```text
~/.autocoding-agent/workspaces/<workspace-id>/
├─ development/
│  ├─ CAPABILITIES.md
│  ├─ pinned/
│  ├─ tasks/
│  └─ capabilities/
└─ incident/
   ├─ CAPABILITIES.md
   ├─ pinned/
   ├─ tasks/
   └─ capabilities/
```

`workspace-id` 由规范化工作区绝对路径计算 SHA-256 前 16 位。能力文件写入前会脱敏工作区路径、
用户主目录和常见密钥形式。任务 JSON 用于幂等判断和重建 `CAPABILITIES.md` 索引。

项目知识源文件仍位于本项目 `knowledge/`。新任务只同步用户选中的项目 MD 到对应能力目录的
`pinned/` 只读视图，避免一次加载所有项目知识。

能力保存属于次要流程：如果任务已经成功但能力落盘失败，任务仍保持 completed，只追加
`capability_failed` 事件。

### 6.15 本地可观测性

日志默认保存到：

```text
~/.autocoding-agent/logs/autocoding-agent.log
```

使用 UTF-8 轮转日志，单文件 2 MB，保留 5 份。日志记录 session ID、模式、模型、工作区、
启动/完成、耗时、Token、超时和脱敏后的 Runtime 错误，不记录完整用户问题、系统 Prompt、
API Key、数据库密码和原始查询结果。

本地日志的定位是“失败后追溯”，不是业务数据归档。

### 6.16 Decision、Artifact 与安全恢复

`core/handlers/` 将 Inspect、Implement、Verify 和 Recovery 阶段拆成独立 Handler；Handler 只返回
结果，不直接修改生命周期。StateMachine 仍是唯一 TaskState 写入口。

每个模型决定会形成 `DecisionRecord`，保存 reason、alternatives、confidence、risk、证据引用、
模型和来源 event ID。`explain_change(task_id, path)` 将相关决定与 Artifact 聚合起来回答“为什么
建议或修改这个文件”，但不会把模型理由伪装为执行事实。

任务 Artifact 位于 `<data_dir>/tasks/<task-id>/artifacts/`，正文使用 UUID 文件名并由 manifest 记录：

- analysis/context/proposal；
- baseline status 与 baseline patch；
- changes patch；
- test result；
- recovery report；
- final report。

每条记录保存 SHA-256、大小、schema version、来源、关联路径和 `host_verified`。写入使用短临时
文件名再原子替换，规避 Windows 长路径；内容经过凭据脱敏并受大小限制。模型 test summary 默认
不是 host verified；只有从真实 Bash ToolResult 观察到的测试命令才产生宿主 `test_executed`。

RecoveryManager 遵循“不能证明安全就不自动重试”：启动扫描只协调死进程或孤儿 run，写阶段
统一进入 recovery_required。用户只能选择只读检查、重新规划或取消；验证失败进入 replanning，
新修改仍需重新审批，超过配置的重规划次数后转为 failed。

---

## 7. 实现过程中遇到的问题与解决方案

### 7.1 过度框架化限制模型能力

**问题**：早期倾向于用宿主代码判断需求是否包含路径、是否应该扫描项目、下一步走哪个固定
节点。规则越来越多，模型只剩下填空角色。

**解决**：重构为小状态机。需求清晰度、相关文件、诊断和方案交给模型；宿主只保留权限、
Schema、状态和路径边界。

**经验**：Agent 系统中的确定性代码应该保护边界，而不是替代模型语义能力。

### 7.2 全项目分析消耗时间和 Token

**问题**：为了“理解项目”默认读取大量文件，旧式单体项目尤其容易产生无关上下文和幻觉。

**解决**：要求模型从用户提供的文件、路径、页面或业务线索开始，只追踪最小必要调用链；信息
不足时询问一个关键问题。项目知识用于提供地图，不替代当前代码验证。

**经验**：好的代码 Agent 不是读得最多，而是知道什么证据足以支持当前结论。

### 7.3 App 内 Skill 无法随目标仓库 cwd 自动发现

**问题**：Claude Code 的 cwd 必须是用户目标仓库。如果把 Skill 只放在应用自己的 `.claude`
目录，运行时通常不会发现。

**解决**：使用 `SkillRegistry` 从 Python 包中显式加载 `SKILL.md`，把工作方法追加到系统 Prompt，
不依赖目标仓库或用户全局配置。

**经验**：应用拥有的 Agent 能力必须显式注入，不能依赖当前目录偶然发现。

### 7.4 Windows 找不到裸 `claude`

**问题**：命令行中 `claude` 可能实际是 `.cmd` 或 `.ps1`。`subprocess.run([...], shell=False)` 在
Windows 下不能保证解析它，PowerShell 还可能阻止 `claude.ps1`。

**解决**：自动搜索并验证真实 `claude.exe`，配置页允许手动选择；每次保存都运行隐藏的
`--version` 检查。默认配置也优先已知真实可执行路径。

**经验**：服务进程调用 CLI 时必须保存可直接执行的文件，而不是保存交互式 Shell 中的别名。

### 7.5 自定义模型名告警但接口实际可用

**问题**：DeepSeek Anthropic 兼容端点使用 `deepseek-v4-pro`，Claude Code 不认识该模型名并
提示上下文窗口告警，但实际请求可以成功。

**解决**：把端点和模型作为用户配置，不在宿主中枚举“合法模型”；以真实健康检查和结构化
输出结果判断可用性。

**经验**：兼容端点场景中，客户端内置模型列表不一定是服务能力真相；但上下文窗口和费用
估算仍可能不准确，需要在生产化前明确配置。

### 7.6 自由文本 JSON 解析不可靠

**问题**：从模型文本中寻找第一个和最后一个花括号容易受 Markdown、解释文本和嵌套内容影响。

**解决**：Claude Code 使用 `--json-schema` 返回 `structured_output`，再由 Pydantic 二次校验；
缺少结构化结果直接失败，不再猜测。

**经验**：凡是要驱动状态机的模型输出，都应使用明确 Schema，而不是依赖提示词保证文本格式。

### 7.7 目标仓库设置可能扩大权限

**问题**：目标仓库或用户的 Claude settings、hooks、MCP、Skills 可能引入额外工具或自动授权，
破坏宿主权限矩阵。

**解决**：使用 `--bare`、空 setting sources、严格空 MCP config 和 `--no-chrome`，只暴露当前
模式声明的内置工具。项目 CLAUDE.md 若被读取，也只是低优先级不可信项目上下文。

**经验**：提示词中的“请勿修改”不是权限边界，工具可见性和外部配置隔离才是。

### 7.8 Capability 目录可能被修改模式写入

**问题**：能力目录位于工作区之外。如果在 Implement 模式仍通过 `--add-dir` 挂载，获得
Edit/Write 权限的模型可能修改长期记忆，形成持久化污染。

**解决**：只在 Inspect 模式挂载 Capability 目录；Implement 和 Verify 不提供该外部目录。

**经验**：长期知识既是数据资产，也是潜在 Prompt Injection 载体；可读与可写权限必须分开。

### 7.9 超时后可能重放有副作用的首轮

**问题**：如果只在模型成功返回后保存 Claude session ID，首轮超时前可能已经修改文件。下一次
继续时应用不知道旧 session，可能把任务作为新会话重放。

**解决**：首轮启动前持久化预分配的 runtime UUID，后续始终精确 resume。

**经验**：Agent 的幂等性不能只看最终返回；必须考虑工具已经执行但结果尚未送达的中间状态。

### 7.10 修改审批缺少可审查方案

**问题**：仅展示“申请修改这些文件”无法让用户判断修改目标，批准行为没有明确语义。

**解决**：引入 `ChangeProposal`，要求描述 before/after、预期结果、影响、验证和可用预览。旧会话
缺方案时禁止直接实施。

**经验**：审批不是一个按钮，而是用户对明确方案和范围的授权。

### 7.11 桌面界面在问答时冻结

**问题**：Runtime 使用同步 subprocess；如果在 Tk 主线程直接调用，模型等待期间窗口无响应。

**解决**：模型调用放到单一后台线程，结果通过队列和 `root.after()` 返回主线程，忙碌期间禁用
重复提交。

**当前状态**：开发 Runtime 已改为持有 `Popen` 句柄并支持按 run ID interrupt；桌面端仍只在持久化
边界提供暂停/取消，避免把“终止当前父进程”误导成“所有子进程和副作用都已撤销”。Windows
Job Object 级进程树清理仍是后续增强。

### 7.12 每次问答弹出 Claude 控制台

**问题**：Windows 子进程默认可能创建控制台窗口，严重影响桌面体验。

**解决**：统一封装 `hidden_window_options()`，Claude 检测和 Runtime 调用都使用隐藏窗口标志；
桌面客户端通过 `pythonw.exe` 启动。

### 7.13 问答超时无法追溯

**问题**：模型调用可能超时。没有生命周期事件时只能看到 UI 错误，无法判断工具是否已经执行、
模型是否返回，以及是否可以安全重试。

**解决**：除本地轮转日志外，Runtime 还逐行产生脱敏活动事件，并持久化 run start、heartbeat、
complete/fail/interrupt。超时后若处于写或验证阶段，任务进入 recovery_required，而不是自动重试。

### 7.14 项目知识 Markdown 逐渐臃肿

**问题**：如果每次开发或异常完成都追加到同一项目 Markdown，文档会混入页面特例、临时状态和
重复结论，降低检索精度并增加 Token。

**解决**：项目 Markdown 只保留稳定的跨页面知识；每次任务的具体经验写入独立 Capability。
开发和异常分开，项目也按二级分支分开。

**经验**：基础知识、任务记录和长期工程经验必须分层，不能把所有记忆放进一个文件。

### 7.15 Capability 文件名和内容脱敏

**问题**：如果用模型生成标题构造文件名，标题中的密钥可能进入文件名和索引；Bearer Token 若
先经过通用 Authorization 正则，也可能只脱敏前缀、留下凭据尾部。

**解决**：Capability 文件使用 session UUID，不使用标题作为路径；Bearer 模式先于通用键值
正则执行，文档内容统一替换工作区、用户目录和常见密钥。

**经验**：脱敏要覆盖内容、文件名、索引、异常和日志；正则执行顺序本身也是安全逻辑。

### 7.16 SQL Server 旧驱动 TLS 握手失败

**问题**：使用系统旧的 `SQL Server` ODBC 驱动连接现代或受策略限制的 SQL Server 时出现
`SSL 安全错误`、`SECDoClientHandshake` 和无效连接串属性。

**解决**：检测并选择 `ODBC Driver 17 for SQL Server`，保留 Encrypt 与
TrustServerCertificate 配置。UI 只展示实际安装的 SQL Server 驱动并优先现代版本。

**经验**：端口可达不等于数据库协议可用；应区分 TCP、TLS/驱动、认证、数据库权限四个层次。

### 7.17 `pyodbc.Cursor.timeout` 属性不存在

**问题**：真实 `pyodbc 5.3.0` 的 Cursor 没有 `timeout`，代码给 Cursor 动态赋值时报：

```text
'pyodbc.Cursor' object has no attribute 'timeout' and no __dict__ for setting new attributes
```

原测试 FakeCursor 允许动态属性，导致缺陷未被发现。

**解决**：在创建 Cursor 前设置 `connection.timeout`；同时使用 `__slots__` 收紧 FakeCursor，测试
断言连接对象的查询超时。`pyodbc.connect(..., timeout=...)` 仍用于登录连接超时，两者职责不同。

**经验**：测试替身不能比真实依赖更宽松；涉及第三方扩展类型时，应模拟其关键限制。

### 7.18 Snapshot 与 Event 半提交

**问题**：如果先改 session 再写事件，或先写事件再保存 snapshot，进程崩溃会产生无法解释的
生命周期；并发入口还可能用旧状态覆盖新状态。

**解决**：SQLiteTaskStore 使用 `BEGIN IMMEDIATE` 在同一事务内分配连续 sequence、追加不可变
Event、写入 Decision/Artifact/Run/Command Receipt 并以 revision compare-and-swap 更新 snapshot。
故障注入测试验证事务回滚后内存 revision 和 sequence 也能恢复。

**经验**：Event Store 的价值不在于“多写一份日志”，而在于事件和当前状态必须共享一致性边界。

### 7.19 崩溃恢复与重复副作用

**问题**：进程在 Edit/Write 后、最终结果前崩溃时，只看 task_state 无法知道副作用是否发生；直接
resume 或重跑可能重复修改。

**解决**：实施前记录 baseline，运行期间保存 run owner/PID/heartbeat，启动时扫描孤儿运行并
比较当前工作区。只读任务可暂停后检查，写/验证任务强制 recovery_required，由用户选择只读
调查、重新规划或取消，绝不自动重放。

**经验**：恢复首先是风险判断，其次才是续跑；“不知道是否执行过”必须建模为独立状态。

### 7.20 Artifact 文件在 Windows 超长路径失败

**问题**：把语义标题和长后缀直接拼进任务产物文件名，会在深层数据目录触发 Windows 路径长度
限制，导致任务主体成功但审计落盘失败。

**解决**：正文统一使用 UUID 文件名，语义类型、来源和关联路径存入 manifest；临时文件也使用
短随机名后原子替换。

**经验**：文件名应是稳定标识，不应承担展示语义；跨平台路径上限必须进入 Artifact 设计。

---

## 8. 技术难点与通用解决方案

### 8.1 自主性与安全性的平衡

最困难的问题不是让模型调用工具，而是既不通过规则削弱模型，又不让模型自行扩大副作用。
当前采用“三层控制”：

```text
模型语义判断
  ↓
结构化数据契约
  ↓
宿主权限与路径/SQL边界
```

三层分别解决“做什么”“如何表达”和“能不能执行”，不能互相替代。

### 8.2 跨进程连续会话

Claude Code 每轮是独立 CLI 子进程，但用户期待连续对话。解决方式是同时保存应用 session 和
Claude session：应用 JSON 保存业务状态，Claude transcript 保存模型上下文，通过精确 ID
恢复。应用历史不是模型会话的替代品，而是产品状态和审计依据。

### 8.3 业务数据进入模型的安全通道

直接给模型数据库工具会扩大权限，也难以保证数据不落盘。当前采用“模型规划、宿主执行、结果
短暂回传、只存审计摘要”的闭环，兼顾语义选择和数据边界。

### 8.4 历史知识复用与上下文污染

完全不读历史会重复调查；把所有历史塞入 Prompt 又会造成 Token 浪费和陈旧知识干扰。当前先用
索引 + 按需打开相关 Capability，下一阶段升级为带来源、状态和适用边界的混合检索。

### 8.5 老项目的证据链

在 WinForms、EF6 和多项目单体解决方案中，页面、服务、仓储和数据库经常分散。项目知识提供
典型调用链和定位方式，但模型必须回到当前工作区核实 Designer、事件、接口、实现、容器注册、
DbContext 和数据库数据，不能只凭历史经验推断。

---

## 9. 技术选型与决策记录

| ID | 决策 | 采用方案 | 未采用/暂缓方案 | 主要原因与代价 |
| --- | --- | --- | --- | --- |
| ADR-001 | 产品边界 | 平台无关 Agent 内核 | 继续建设重型工作流平台 | 减少平台复杂度，保留模型能力；需要清晰端口 |
| ADR-002 | 语义判断 | 交给模型 | 文件名/关键词硬编码规则 | 适应不同项目；结果必须结构化校验 |
| ADR-003 | Agent Runtime | Claude Code CLI stream-json | 自研完整 Tool Loop | 快速复用成熟能力并获得活动事件；需维护 CLI 协议兼容 |
| ADR-004 | 模型输出 | JSON Schema + Pydantic | 解析自由文本 JSON | 状态机可靠；Schema 设计需要谨慎演进 |
| ADR-005 | 会话恢复 | 精确 session ID / resume | continue 或重放历史 | 防止同目录串线和重复副作用 |
| ADR-006 | 权限控制 | Inspect/Implement/Verify 分档 | 单一高权限模式 | 用户审批清晰；多一轮交互 |
| ADR-007 | 修改审批 | 结构化 before/after 方案 | 仅显示操作列表 | 授权可理解；模型输出契约更复杂 |
| ADR-008 | 异常流程 | 只读诊断 | 首版自动修复/写库 | 先建立可信证据链；暂时不能闭环处置 |
| ADR-009 | 数据库调用 | 模型提查询、宿主执行 | 直接给模型 DB 工具 | 可统一校验、限行和脱敏；需要查询状态循环 |
| ADR-010 | 桌面技术 | Tkinter/ttk | PySide、CustomTkinter、WebView | 零大型依赖、无本地服务；视觉和富文本能力有限 |
| ADR-011 | 配置秘密 | 用户环境变量 + Credential Manager | `.env` 明文 | 不污染仓库；跨平台一致性较弱 |
| ADR-012 | 初版会话存储 | 原子 JSON 文件 | 首版直接引入数据库 | 早期易检查、易迁移；现由 SQLite 兼容导入 |
| ADR-013 | Capability | 工作区 + 流程隔离 | 自动覆盖 CLAUDE.md | 不污染目标仓库；跨工作区尚不能共享 |
| ADR-014 | 项目知识 | 每流程、每项目一份 MD | 一个全局大文档 | 控制上下文和维护边界；需用户选择项目 |
| ADR-015 | 日志 | 本地轮转脱敏日志 | 无日志或记录完整 Prompt | 可追溯且控制泄密；诊断信息有意受限 |
| ADR-016 | 发布策略 | 每次完成迭代验证、提交并推送一次 | 多轮本地积累后批次推送 | 每个迭代都有远端回退点；要求推送前同步版本和文档 |
| ADR-017 | 工程经验 | 候选审核后进入统一知识库 | 完成即直接写正式经验 | 降低重复、错误泛化和知识污染；增加治理步骤 |
| ADR-018 | 任务生命周期 | TaskState、AgentStatus、AgentMode 三层分离 | 继续复用单一 status 字段 | 状态语义清晰并支持恢复；需要旧会话迁移层 |
| ADR-019 | 状态转换 | Command + StateMachine + expected version | Engine 分散直接赋值 | 可审计、可校验，并由原子 Event Store 保证持久化一致性 |
| ADR-020 | Task/Event 存储 | SQLite snapshot + append-only event 同事务 | JSON snapshot + 独立 JSONL | 保证一致性和并发拒绝；增加迁移与 schema 管理 |
| ADR-021 | 执行事实 | 宿主 diff/ToolResult 与模型自报分离 | 直接信任 changed_files/test_summary | 避免虚假审计；需额外采集 Artifact |
| ADR-022 | 任务恢复 | 写阶段转 RecoveryRequired，禁止自动重放 | 启动后无条件 resume | 避免重复副作用；需要用户恢复决策 |
| ADR-023 | Runtime 观察 | CLI stream-json + Popen interrupt | 立即迁移 Agent SDK | 与现有 DeepSeek/Claude Code 配置兼容；进程树治理有限 |
| ADR-024 | Artifact 命名 | UUID 正文 + manifest 元数据 | 语义长文件名 | 避免 Windows 长路径并便于不可变校验 |
| ADR-025 | 重规划 | 验证失败有上限地 replanning 并重新审批 | 无限制自动 repair | 防止失控循环；增加一次人工授权往返 |

---

## 10. 测试策略

### 10.1 确定性测试优先

默认测试不调用模型、不消耗 API Token。通过 Fake Runtime、Fake Connection、临时目录和结构化
结果覆盖状态机与边界。目前测试涵盖：

- 需求澄清和同会话继续；
- 修改/验证审批、拒绝和旧审批兼容；
- 方案字段和预览展示；
- Inspect/Implement/Verify 工具矩阵；
- Claude 命令构造、session resume 和错误分类；
- 开发和异常数据库查询循环；
- SQL 参数绑定、只读拦截、限行和敏感列脱敏；
- SQL Server 配置和密码回滚；
- Project Knowledge 路径、分支、同步和隔离；
- Capability 生成、幂等、索引和秘密脱敏；
- 桌面 UI 路由、忙碌状态、流程选择和审批卡；
- 日志轮转和 Runtime 错误脱敏；
- 状态转换、命令幂等、Event replay、并发 revision 和事务回滚；
- Handler、Decision、Artifact、clean/dirty/non-Git 工作区策略；
- Runtime stream 活动、interrupt、真实 ToolResult 与模型自报分离；
- 孤儿运行扫描、RecoveryRequired、暂停/取消和重规划上限。

### 10.2 Live 测试隔离

真实 Claude Code/模型调用标记为 `live`，默认完整检查使用：

```powershell
D:\python\python.exe -m pytest -m "not live" -q
D:\python\python.exe -m ruff check src tests
```

需要验证真实模型兼容性时单独运行 live 测试，避免日常回归测试产生费用和不确定性。

### 10.3 测试经验

- 测试输出契约，不测试某个模型一定会说什么；
- Fake 必须保留真实依赖的关键限制；
- 安全缺陷应增加负向测试，而不只验证 happy path；
- UI 尽量把格式化和路由提取成纯函数测试；
- 模型 Prompt 变化需要同时验证 Runtime Schema 和状态机兼容；
- 生命周期转换必须覆盖合法、非法、幂等、旧版本和旧 session 推导；
- 实际集成问题仍需要分层测试网络、驱动、认证和权限，单元测试不能替代真实环境。

---

## 11. 配置、运行与发布

### 11.1 运行要求

- Python 3.12+；
- Claude Code；
- 可用的 Anthropic 兼容端点、模型和 API Key；
- 需要 SQL Server 时安装 ODBC Driver 17 或 18。

根目录双击：

```text
start.cmd
```

脚本优先使用 `D:\python\python.exe`，否则从 PATH 寻找 Python；检查版本、依赖和 Tkinter 后默认
启动客户端。调试时可使用：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Wait
```

### 11.2 关键环境配置

| 变量 | 含义 |
| --- | --- |
| `AUTO_CODING_CLAUDE_COMMAND` | 真实 Claude Code 可执行文件 |
| `AUTO_CODING_CLAUDE_MODEL` | 模型名 |
| `ANTHROPIC_BASE_URL` | Anthropic 兼容 API 地址 |
| `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` | API 凭据 |
| `AUTO_CODING_CLAUDE_TIMEOUT_SECONDS` | 单轮外层超时 |
| `AUTO_CODING_DATA_DIR` | 本地会话、能力和日志根目录 |
| `AUTO_CODING_DATABASE_MAX_ROWS` | 数据库单次返回上限 |
| `AUTO_CODING_DATABASE_QUERY_TIMEOUT_SECONDS` | SQL 查询超时 |
| `AUTO_CODING_DATABASE_MAX_QUERY_ROUNDS` | 单任务查询轮数上限 |
| `AUTO_CODING_AGENT_MAX_REPLAN_ROUNDS` | 验证失败后允许的最大重规划轮数 |
| `AUTO_CODING_RUNTIME_LEASE_SECONDS` | 启动扫描判断运行租约的时间窗口 |

### 11.3 发布与回退

从 `0.4.0` 开始，每次完成迭代都同步版本号、README、架构、接口和工程经验文档，运行完整测试与
静态检查，创建一个边界清晰的提交并向 GitHub 推送一次。功能不兼容或架构升级递增 minor，兼容
修复递增 patch；需要形成公开里程碑时再创建不可变 tag。

回退优先从历史提交或 tag 新建安全分支，不使用 `git reset --hard`、强制推送和覆盖 tag。发布
前必须检查工作区，禁止提交 `.env`、本地日志、缓存、凭据和目标项目生成文件。

---

## 12. 安全模型与风险边界

### 12.1 主要受保护资产

- 用户目标仓库；
- 数据库业务数据；
- API Key 和数据库密码；
- Capability 和未来工程经验知识；
- Claude session 与应用会话完整性；
- 本地日志和用户隐私。

### 12.2 主要威胁

- 模型越过用户批准范围修改文件；
- 目标仓库 CLAUDE 设置、MCP 或历史知识扩大权限；
- 数据库查询包含写操作或大规模数据读取；
- 数据库行、代码注释或能力文档中的 Prompt Injection；
- 密钥通过错误、日志、文件名或 Markdown 索引泄漏；
- 超时后重复执行有副作用任务；
- 多进程并发覆盖会话；
- 模型报告的变更与真实 Git diff 不一致；
- 陈旧经验被当作当前事实。

### 12.3 已有防护

- 工具按模式收缩；
- 修改和验证前用户审批；
- Claude 设置/MCP 隔离；
- 结构化输出和路径校验；
- 只读 SQL 校验、参数绑定、限行、超时和脱敏；
- 密钥分离存储和日志脱敏；
- 预分配 session ID；
- 原子文件写入；
- SQLite 原子 Task/Event 提交、revision 并发拒绝和命令 receipt；
- Runtime owner/PID/heartbeat、启动孤儿扫描和进程级 interrupt；
- Implement 前后 Git status/diff Artifact 与模型自报分离；
- 写阶段中断进入 RecoveryRequired，禁止自动重放；
- 历史知识不可信声明；
- 能力目录只在只读模式挂载。

### 12.4 尚待补强

- Windows Job Object/完整进程树清理；
- 分布式运行所有权和跨机器租约；
- SDK 级逐工具审批和更稳定的协议适配；
- SQL Parser 级只读验证，而不只依赖保守正则；
- 数据库账号最小权限自动检查；
- Engineering Knowledge 的审核、撤销、版本和证据机制。

---

## 13. 当前限制与技术债

1. 应用门面仍按轮次同步返回，没有向 UI 展示流式 Token；
2. Runtime 可终止登记的 Claude 父进程，但不能完全证明其所有后代进程都已清理；
3. 运行租约使用本地 owner/PID/heartbeat，不是跨机器分布式锁；
4. Git 工作区可采集真实 diff，非 Git 工作区只能记录状态，无法生成等价 patch；
5. Verify 只从已识别的真实 Bash ToolResult 形成宿主测试事实，复杂命令可能需扩展识别；
6. SQL 只读检查是保守规则，可能拒绝合法复杂查询，也不能替代只读数据库账号；
7. 数据 schema 每个只读轮次都会读取，较大数据库需要缓存和按需裁剪；
8. Capability 是“每完成任务一份文档”；RAG 能发现内容变化，但尚未做语义去重、合并和知识审批；
9. 开发与异常已经共享 Event/Run/Recovery 基础设施，但两个 SQLite store 仍有部分事务代码重复，
   后续可提取通用持久化基类；
10. Voyage 未配置时 Dense 排名仍来自 `fake-hash-embedding-v1`；正式 Voyage 已可用，但当前
    SQLite VectorStore 是线性扫描，不适合大规模向量；
11. Project Knowledge 依赖人工维护，当前没有冲突检测和版本审批；
12. Tkinter 的 Markdown、富文本和可访问性能力有限；
13. 异常流程只输出建议，尚未形成人工确认后的受控处置状态机；
14. 尚无钉钉鉴权、幂等、回调签名、任务关联和通知重试；
15. 模型兼容端点的上下文窗口、费用和未知模型提示仍需独立管理。

这些限制应进入后续版本规划，不能通过 Prompt 声称已经解决。

---

## 14. 下一阶段：Engineering Experience Knowledge

### 14.1 目标

从“每个工作区复用自己的 Capability”升级为“跨项目复用经过治理的工程经验”。经验内容包括：

- 通用技术方案；
- 架构决策及原因；
- 故障排查方法；
- 失败案例和反模式；
- 最佳实践及适用边界。

Failure Knowledge 建议作为 Engineering Knowledge 的 `failure`/`anti_pattern` 类型，而不是再
建设一套完全独立的存储和检索系统。

### 14.2 推荐知识模型

每条知识应至少包含：

```text
id / type / title / summary
problem_signals / context
decision / rationale / method
validation / failure_modes / constraints
evidence_refs / tags / confidence
status / created_at / last_verified_at / supersedes
```

必须回答：何时适用、为什么这样做、证据是什么、何时不要使用。

### 14.3 沉淀闭环

```text
任务完成
  ↓
Capability / Incident / Failure Log
  ↓
统一转换为 Knowledge Candidate
  ↓
脱敏、质量校验、相似度检测
  ├─ 重复：合并或增加证据
  ├─ 冲突：保留不同上下文和边界
  └─ 新知识：进入候选区
  ↓
人工确认或达到证据门槛
  ↓
Engineering Knowledge(verified)
  ↓
后续任务检索与引用
```

第一阶段不应让任务完成后直接写入正式知识库。Capability 是来源，Knowledge Candidate 是缓冲，
verified Engineering Knowledge 才是可长期复用的正式经验。

### 14.4 检索方案

`v0.6.0` 先用 SQLite 保存 Chunk/FTS5，并以可替换的伪 Embedding 打通混合召回；`v0.6.1`
已接入可配置 Voyage。后续沿用同一结构升级外部向量数据库和评估层：

```text
FTS 精确词检索 ─┐
                  ├─ 合并 → 元数据过滤 → 重排 → Top 3~5
向量语义检索 ────┘
```

工程问题包含错误码、类名、方法名和框架名，纯向量检索并不可靠。索引是可重建派生数据，结构化
知识及版本记录才是真相来源。

### 14.5 工作流接入位置

需求明确并确认基本技术上下文后再检索工程经验，不应在用户每输入一句话时加载大量知识。

```text
理解任务
  → 确认页面/模块/技术栈/错误类型
  → 生成知识查询
  → 检索少量相关经验
  → 模型结合当前代码和数据制定方案
```

检索结果必须携带知识 ID、状态、来源和最后验证时间，并继续作为不可信参考。最终方案最好说明
使用了哪些经验，便于评估知识是否真正产生价值。

### 14.6 实施状态与建议路线

- `v0.6.0`：人工管理、Markdown Chunk、FTS5 + 模拟向量 + RRF、开发/异常注入和引用事件；
- `v0.6.1`：Voyage 配置/测试、正式 REST Adapter、OS 密钥、索引隔离和手动重建；
- `v0.6.2`：异常流程页面名称前置、截图标题识别、项目映射分层和有界候选验证；
- `v0.6.3`：对话先于图片、标题/路径联合入口、候选与截图冲突澄清；
- `v0.6.4`：双流程实时进度投影与持久状态分层；
- `v0.7.0`：Hermes Skill 只读工程经验端口、双流程回灌、事件/Artifact 和失败降级；
- `v0.7.1`：ACE 到 Hermes 的 DeepSeek provider 安全桥接、独立 Flash 模型和真实联调；
- `v0.7.2`：Claude启动配置刷新、失效路径恢复、启动目标预检和明确错误分类；
- `v0.7.3（当前）`：大提示词改用临时文件、用户消息改用stdin，并增加Windows命令行长度预检；
- 下一检索迭代：外部 VectorStore、健康检查、批量迁移和真实检索评测集；
- 治理迭代：统一 Engineering Knowledge/Candidate 模型，把 Capability、异常与失败日志转为候选；
- 质量迭代：脱敏、去重、冲突检测、陈旧标记、人工审核与基于证据的晋级；
- 评估迭代：真实 reranker、检索评测集、引用准确率和任务效果反馈闭环。

### 14.7 成功指标

知识条数不是主要指标，应关注：

- 检索 Top-K 相关率；
- Agent 对知识来源的引用准确率；
- 使用经验后任务成功率和方案接受率；
- 重复调查和重复失败是否减少；
- 平均输入 Token 是否下降；
- 错误或过期知识被采用的比例；
- 候选重复率、合并率、停用率；
- 同一经验在不同项目成功复用的次数。

---

## 15. 关键工程经验总结

1. **模型做语义判断，代码做确定性边界。** 不要用简单规则替代需求理解，也不要用 Prompt
   替代权限控制。
2. **先有证据，再有方案。** 文件、路径、当前代码、schema 和授权数据是结论基础。
3. **审批必须对应清晰方案。** 用户批准的是明确 before/after 和范围，不是抽象的“允许修改”。
4. **模型输出必须结构化。** Prompt 约定不能代替 Schema 和宿主校验。
5. **会话恢复要考虑半完成副作用。** 超时不代表工具从未执行，session ID 必须在启动前持久化。
6. **历史知识是参考，不是事实。** Project Knowledge、Capability 和未来 RAG 都要以当前证据复核。
7. **不同生命周期的知识必须分层。** 项目事实、任务状态、能力和跨项目经验不能混在一个 MD。
8. **数据库安全需要多层防线。** 文本校验、参数绑定、限行、脱敏、只读意图和只读账号缺一不可。
9. **秘密可能从任何载体泄漏。** 不只检查正文，还要检查文件名、索引、日志、异常和正则顺序。
10. **测试替身必须忠于真实依赖。** 过于宽松的 Fake 会把集成缺陷隐藏到用户环境。
11. **UI 不应伪造 Runtime 能力。** 进程终止不等于副作用回滚，恢复界面必须呈现真实风险。
12. **平台入口与业务内核解耦。** 新入口只调用应用门面，不复制 Prompt、状态机和安全逻辑。
13. **知识积累需要治理。** 自动生成只能进入候选区，正式经验要有证据、边界、版本和退出机制。
14. **版本可回退比版本数量更重要。** 每次迭代只形成一个经验证的远端提交，边界清晰才便于回退。
15. **模型理由与宿主事实必须分开。** changed_files 和 test_summary 是声明，diff 和 ToolResult 才是证据。
16. **恢复不能等同于重试。** 写操作结果不确定时先进入 RecoveryRequired，再由证据和用户决定。

---

## 16. 后续维护指南

### 16.1 修改状态机时

- 先修改 Pydantic 契约；
- 再修改集中 StateMachine 的转换规则和对应 Handler；
- 同步 Runtime Schema、UI 显示和 CLI 输出；
- 增加合法和非法状态的确定性测试；
- 说明旧会话如何兼容或明确拒绝。

### 16.2 增加新工具权限时

- 先说明它属于 Inspect、Implement 还是 Verify；
- 判断工具是否有间接副作用；
- 不把 `allowed_tools` 当作唯一可见性边界；
- 检查目标仓库设置、MCP 和外部目录是否可能扩权；
- 添加越权负向测试。

### 16.3 增加数据库能力时

- 优先扩展 `DatabaseReader`，不要让模型直接持有凭据；
- 明确只读/写入边界；
- 查询必须参数化、限时、限行、脱敏；
- 原始业务数据默认不持久化；
- 生产环境使用最小权限账号；
- 记录查询审计而不是完整结果。

### 16.4 增加知识能力时

- 先定义知识生命周期和来源；
- 区分项目事实、任务记录、候选经验和正式经验；
- 所有知识带来源、状态和适用边界；
- 索引必须可重建；
- 注入模型时限制 Top-K 和 Token；
- 冲突和陈旧知识应停用或降权，不静默覆盖历史。

### 16.5 发布前检查

```powershell
git status --short
D:\python\python.exe -m pytest -m "not live" -q
D:\python\python.exe -m ruff check src tests
```

同时确认：

- 未提交 `.env`、凭据、日志、缓存和数据库结果；
- 版本号、README、架构和接口文档与代码一致；
- 当前迭代的任务卡和本工程经验文档已同步；
- 新功能有失败路径和边界测试；
- 已发布 tag 没有被修改；
- 回退路径明确；
- 当前迭代只创建一个边界清晰的提交，并在检查后向 GitHub 推送一次。

---

## 17. 相关文档与关键代码

### 文档

- `README.md`：使用方式和产品概览；
- `docs/ARCHITECTURE.md`：当前系统架构；
- `docs/INTERFACES.md`：API、枚举、数据模型和 CLI 契约；
- `RELEASING.md`：发布和回退；
- `docs/tasks/ACE-RUNTIME-001-agent-engine-runtime-upgrade.md`：本次 Runtime 升级任务记录；
- `docs/tasks/ACE-INCIDENT-003-event-recovery-autonomous-sql.md`：异常恢复与自治 SQL 迭代记录；
- `knowledge/`：分流程、分项目的用户维护知识。

### 关键代码

- `src/autocoding_agent/core/engine.py`：开发状态机；
- `src/autocoding_agent/core/state_machine/`：任务生命周期、命令、失败分类和转换规则；
- `src/autocoding_agent/core/handlers/`：Inspect、Implement、Verify、Recovery 阶段处理器；
- `src/autocoding_agent/core/audit/`：Decision Record 与修改原因聚合；
- `src/autocoding_agent/core/artifacts/`：任务产物契约和采集；
- `src/autocoding_agent/core/recovery/`：孤儿运行扫描与保守恢复；
- `src/autocoding_agent/adapters/sqlite_task_store.py`：原子 Task snapshot、追加事件、回放与旧 JSON 导入；
- `src/autocoding_agent/adapters/sqlite_incident_store.py`：异常 snapshot/Event/Run/Command 事务存储；
- `src/autocoding_agent/adapters/task_artifact_store.py`：脱敏、校验和原子 Artifact 落盘；
- `src/autocoding_agent/adapters/workspace_snapshot.py`：Git 工作区基线与差异观察；
- `src/autocoding_agent/incident/engine.py`：异常诊断状态机；
- `src/autocoding_agent/incident/recovery.py`：异常孤儿 Runtime 的暂停与恢复策略；
- `src/autocoding_agent/adapters/claude_code.py`：Claude Code Runtime；
- `src/autocoding_agent/core/policies.py`：工具权限矩阵；
- `src/autocoding_agent/adapters/sqlserver_database.py`：SQL Server 只读适配器；
- `src/autocoding_agent/adapters/database_safety.py`：SQL 安全校验与脱敏；
- `src/autocoding_agent/adapters/capability_store.py`：开发能力记忆；
- `src/autocoding_agent/incident/capability_store.py`：异常能力记忆；
- `src/autocoding_agent/workspace_knowledge.py`：项目 Markdown 分支管理；
- `src/autocoding_agent/interfaces/desktop_ui.py`：原生桌面客户端；
- `src/autocoding_agent/interfaces/system_settings_ui.py`：统一配置页面；
- `src/autocoding_agent/model_setup.py`：Claude Code 和模型配置；
- `src/autocoding_agent/sqlserver_config.py`：SQL Server 配置和凭据；
- `src/autocoding_agent/observability.py`：本地轮转日志。

---

## 18. 结语

AutoCodingEngineerCoreNew 当前最重要的成果不是某个 UI，也不是某条 Prompt，而是建立了一个
清晰的责任分界：让模型充分理解和处理工程问题，同时由宿主守住权限、状态、数据和持久化。

下一阶段应在这个边界上建设 Engineering Experience Knowledge，使 Capability、异常记录和失败
日志先转成可审核的知识候选，再通过统一模型、混合检索和证据闭环影响后续任务。最终目标不是
简单增加历史文档数量，而是让 Agent 在更多项目实践后，能够更快找到相关经验、更少重复踩坑，
并且始终说明经验来自哪里、为何适用、何时失效。

---

## 19. 桌面 AI 工程工作台设计迭代

### 19.1 背景与目标

原桌面端已经具备开发/异常双流程、项目知识选择、审批、恢复和配置入口，但视觉层级主要依靠
普通矩形控件，任务导航、对话阅读和当前执行状态之间的主次关系不够明确。本次迭代不改变
Agent 权限与业务状态，只重构信息层级和交互反馈，使长任务更容易阅读和审计。

### 19.2 设计决策

- 采用约 260 px 左侧任务导航、居中对话区和底部任务输入区，保持工程工具的稳定空间模型；
- 参考现代 AI Coding Agent 的输入结构，把知识项目、工作区、页面线索和问题放在同一个任务
  上下文区域，但不隐藏权限边界；
- 使用语义颜色令牌，而不是在控件中散落颜色值；状态不能只靠颜色，必须同时展示状态文字；
- 每个表面只保留一个主操作，审批、恢复和普通导航使用较低强调等级；
- 使用 11–12 px 圆角、1 px 边框和克制阴影感的分层表面，不使用装饰性渐变和无意义图标；
- 自定义 `RoundedButton` 保留键盘焦点、Enter/Space 激活、禁用态和 hover 反馈。

### 19.3 实现难点与经验

Tkinter `Canvas` 内部已经存在 `_options()` 方法。自定义圆角按钮最初把业务选项保存在同名
`_options` 字典中，覆盖了 Tkinter 绘图所需的方法，最终在 `create_polygon()` 中触发
`TypeError: 'dict' object is not callable`。修复方式是使用 `_button_options` 保存控件状态，并用
聚焦测试覆盖实际 Canvas 绘制。经验是：继承 GUI 框架控件时，私有名称也可能是框架协议的一部分，
新增字段前应检查基类命名，并通过真实控件构造测试而不是只测纯函数。

完整设计提示词、令牌和来源见 `docs/UI_DESIGN_GUIDE.md`。

---

## 20. 异常流程迁移到通用 Event/Recovery 与自治 SQL

### 20.1 背景问题

异常流程原本已经能让模型返回 `DataQuery` 并由宿主执行，但生命周期只保存在
`IncidentSession.status` 和原子 JSON 中：状态变化没有连续事件、Runtime 没有 run lease，进程中断
后无法区分“未开始”和“已经执行过只读查询”。同时，旧 Prompt 没有明确禁止模型把 SQL 当作
操作步骤交给用户，页面定位契约还要求查询前必须已有 `LocatedPage`，与 MES 使用 Menu 表把
页面名称映射为 URL 的真实场景冲突。

### 20.2 核心实现

- `IncidentSession` 增加 `TaskState`、version/revision、`AgentEvent`、`RuntimeRunRecord` 和
  `CommandReceipt`；所有状态转换经过共享 `AgentStateMachine`；
- `SQLiteIncidentStore` 在共用 `agent-runtime.db` 中使用 `incident_*` 表，把 snapshot、新事件、
  run 和 command receipt 放在一个事务中，并支持 sequence 回放和旧 JSON 幂等导入；
- `OrphanedRunScanner` 从开发专用恢复逻辑中抽出，开发与异常 Recovery Manager 共用 owner/PID/
  heartbeat 判定；异常 Runtime 永远只读，因此孤儿运行进入 `PAUSED`，但仍不会自动重放查询；
- `IncidentDecision.query_required` 允许暂时没有 `LocatedPage`，模型可以先生成参数化 Menu 映射
  查询，再把返回 URL 作为不可信相对线索去阅读代码；
- Prompt 明确要求从相关代码和 schema 中提取已有查询语义、形成最小 SQL，并禁止要求用户运行
  SQL 或粘贴结果；宿主自动调用 `DatabaseReader.execute()`；
- 查询失败不会立刻把问题甩给用户：宿主把脱敏错误发回同一模型会话自动修正，达到查询尝试
  上限后才形成可解释失败；
- 查询审计只保存 SHA-256 SQL 指纹、参数名、用途、返回行数、截断和脱敏列，不保存参数值、
  原始 SQL 决定或业务行。

### 20.3 技术难点与解决方案

**不同业务 aggregate 如何共享内核。** 开发使用 `AgentSession/AgentDecision`，异常使用
`IncidentSession/IncidentDecision`，强行合并会让字段大量可空。解决方案是让状态机依赖最小
`LifecycleSession` Protocol，让恢复扫描只依赖 task state 和 run lease；业务结果仍由各自 Engine
处理。共享的是事实与安全机制，不是把不同业务契约压成一个模型。

**查询可能在页面定位之前发生。** 原契约把“页面已定位”当成查询前置条件，但 Menu 映射本身就是
定位证据。解决方案是允许 `query_required.page=None`，同时完成态仍强制包含页面和诊断，防止
系统以半成品结束。

**SQL 自动修正不能绕过安全边界。** 模型可以根据脱敏错误修改 SQL，但每次尝试仍重新经过
SELECT/WITH 校验、参数绑定、限时、限行、脱敏和数据源引用检查；失败尝试计入统一轮次上限。
模型自治扩大的是调查能力，不是数据库权限。

**事件审计不能泄漏业务数据。** 保存完整 SQL 和参数会让订单号、人员信息等进入长期日志。
因此事件只保存不可逆查询形状指纹与参数名，原始限行结果只进入当前模型会话。需要追查时可以
确认“哪个查询形状何时执行、返回多少行”，但不能从审计库还原业务值。

### 20.4 决策记录

- ADR-023：异常流程共享 TaskState/Event/Run/Recovery 核心，保留独立业务决定和 SQLite 表；
- ADR-024：数据库调查由模型提出结构化 SQL、宿主自动执行，不把查询步骤交给用户；
- ADR-025：只读 Runtime 中断进入 paused 且需显式恢复，不因“理论无写入”而自动重放；
- ADR-026：查询审计保存指纹和元数据，不长期保存 SQL 参数或业务行。

### 20.5 验证

确定性测试覆盖页面未定位前的 Menu 查询、宿主自动执行、SQL 失败自动修正、事件顺序与状态回放、
SQLite 乐观并发拒绝、异常孤儿 run 启动暂停和显式恢复，以及桌面异常恢复卡。发布前完整非 live
回归为 120 项通过，Ruff 与 diff 检查通过。

---

## 21. Vision Glass 原生桌面工作台迭代

### 21.1 背景与目标

`v0.4.1` 已经完成浅色工作台和圆角按钮，但主窗口仍是“侧栏 + 单列内容”，信息密度和高级桌面
产品感与用户提供的 Apple Vision Pro 风格参考图有差距。`v0.5.1` 只调整界面呈现：建立左侧任务
导航、中部对话/上下文、右侧运行概览的三栏结构，并用白银背景、低对比悬浮层、20–24 px 大
圆角和 Apple Blue 主操作建立统一视觉。Agent 状态机、审批、恢复和数据库安全边界保持不变。

### 21.2 产品与数据决策

- 参考图中的任务指标是视觉样例，正式界面不能复制固定数字；概览读取当前流程真实 Session，
  分别计算今日任务、已完成、进行中、完成率和近七日创建趋势；
- 运行状态读取本地引擎、当前项目知识、模型配置和 SQL Server 配置，不尝试把配置状态冒充为
  实时网络健康检查；
- 宽度低于 1180 px 时隐藏概览区，优先保证任务输入和审批恢复；
- 模型 `inspect()` 会运行一次隐藏的 `claude --version`，因此在窗口生命周期内缓存就绪状态，
  保存模型配置时再主动更新，避免每次刷新历史任务都启动子进程；
- 不新增虚构图标、装饰图和指标，主路径的所有按钮继续调用原有真实行为。

### 21.3 核心技术实现

新增原生 `GlassPanel`：以 Canvas 平滑多边形绘制低对比阴影、白色高光边界和浅色材质面，再通过
`create_window` 承载普通 Tk 控件。这样不引入 PySide/WebView，也能统一顶栏、对话、输入、概览、
侧栏和底部状态的圆角层次。`RoundedButton` 和流程胶囊同步升级到 44 px 高度、更柔和的 14–19 px
圆角与 hover/focus 反馈。七日趋势用 Canvas 读取真实计数绘制，空数据仍显示稳定的零基线。

系统配置页共用新的白银、正文、弱边界和 Apple Blue 令牌；输入框从硬边框改为焦点高亮的轻量
表面。主界面新增输入占位文案，开发与异常流程分别提示适合的任务信息。

### 21.4 实现问题与解决方案

**Tkinter 没有真实毛玻璃。** Tk 8.6 不支持 `backdrop-filter`、RGBA 控件或系统材质采样。如果为
追求真实模糊而引入第二套 Web UI，会扩大运行和维护边界。本次明确采用“材质近似”：多层浅色、
白色内高光、柔和阴影和大圆角；文档中不把它描述为真实光学模糊。

**Canvas 自动高度出现 1 px 容器和子控件越界。** `create_window` 中的 Frame 已经有请求高度，
但 Canvas 初始请求高度仍为 1 px；子控件会绘制到 Canvas 边界之外，造成一个巨大的空白层覆盖
主界面。解决方式是在 `after_idle` 阶段读取 `content.winfo_reqheight()`，再设置 Canvas 高度，并在
内容变化时重新调度测量。经验是：嵌入式 GUI 容器必须同时处理“子控件请求尺寸”和“外层布局
尺寸”，仅依赖 `<Configure>` 事件可能因初始未映射而不触发。

**设计稿与产品事实有冲突。** 视觉稿展示 12 个今日任务、92% 成功率等示例值。若照图硬编码会
让用户误判系统运行状态。解决方式是匹配视觉层级但替换为可解释的真实口径，并为统计函数增加
确定性 UI 测试。

### 21.5 验证结论

本次迭代使用同一原生窗口分别检查宽屏三栏与紧凑布局，并把参考图和实现截图放入同一比较图
完成设计 QA。自动化验证覆盖主题、玻璃容器构造、真实指标统计、响应式隐藏、双流程路由、审批、
恢复和系统配置；最终结果以任务卡和 `design-qa.md` 为准。

---

## 22. 数据库查询预算：60 秒超时与首轮 100 条

### 22.1 背景与业务目标

开发和异常处理都允许模型提取结构化只读 SQL 并由宿主自动查询。原默认值是 5 秒和 50 行：
5 秒对企业 SQL Server 的跨表诊断偏短，容易把正常但稍慢的查询误判成失败；与此同时，如果只把
超时放宽而不明确控制结果规模，模型可能在数量未知时生成无界查询，增加数据库负担并挤占模型
上下文。因此本次把“等待多久”和“最多取多少”作为同一个查询预算设计：默认允许执行 60 秒，
结果数量未知时先查看 100 条，足够判断时继续使用更小样本。

### 22.2 核心实现

- `database_models.py` 定义共享的 100 行和 60 秒默认值，`Settings`、SQL Server Reader 与
  SQLite Reader 引用同一来源，避免两套流程或不同适配器逐渐漂移；
- `DataQuery.max_rows` 默认改为 100，结构化契约仍把单次请求硬限制在 1–100；宿主 Reader 再取
  `min(query.max_rows, configured_max_rows)`，因此用户可以通过配置进一步收紧，不能把模型结果
  放大到 100 行以上；
- Reader 使用 `fetchmany(row_limit + 1)`，第 101 条只用于判断 `truncated`，真正进入当前模型
  轮次的业务行仍不超过 100；
- SQL Server 在每次 metadata/query 执行前设置 `connection.timeout = 60`；连接建立时的 login
  timeout 继续来自 SQL Server 连接配置，两者职责不混用；
- 开发与异常 Prompt 同时增加模型规则：数量未知时先形成 100 条有界采样，并在语义允许时使用
  对应方言的 `TOP`/`LIMIT`；如果 1 条聚合结果或更小样本已够用，应主动选择更小上限。

### 22.3 技术难点与解决方案

**客户端限行不等于数据库端限行。** `fetchmany(101)` 能保护模型上下文，但数据库仍可能执行一条
返回海量数据的语句。宿主无法安全地给任意 SELECT、CTE、聚合或窗口查询机械注入 `TOP`。因此
保留确定性的客户端硬边界，同时把 SQL 级 `TOP`/`LIMIT` 交给理解查询语义的模型；两层各自解决
可验证的安全问题。

**查询超时和连接超时不是一个概念。** pyodbc 连接函数的 `timeout` 用于登录，而 SQL 执行超时
由 connection 的 `timeout` 属性控制；`Cursor` 在当前 pyodbc 版本没有该属性。将二者混用既可能
无法中止慢查询，也会复现 `pyodbc.Cursor has no attribute timeout`。实现中连接建立后、游标执行前
设置执行超时，登录超时保持原配置。

**100 条是默认采样，不是鼓励每次取满。** 将默认值改为 100 可以覆盖数量未知的首轮调查，但
页面映射、计数或唯一键查询通常只需更少数据。Prompt 明确“能少则少”，`DataQuery` 也允许 1–100
的显式值，从而在信息充足度和负载之间保持模型可判断的空间。

### 22.4 决策记录

- ADR-027：开发与异常处理共用数据库查询默认值，默认 100 行、60 秒；
- ADR-028：结果硬边界由宿主 `fetchmany(limit + 1)` 保证，SQL 级有界采样由模型按方言与语义生成；
- ADR-029：SQL Server login timeout 与 query timeout 分离，后者只设置在 pyodbc connection；
- ADR-030：100 行是数量未知时的默认首轮预算，允许模型和用户配置进一步收紧。

### 22.5 验证策略

聚焦测试分别确认共享 Settings 默认值、`DataQuery` 默认值、SQL Server connection 超时、实际
`fetchmany(101)` 调用，以及 SQLite 对 105 条测试数据只返回 100 条并标记截断。发布前还需运行
完整非 live 回归、Ruff 与 diff 检查；最终结果记录在 `docs/tasks/ACE-DATA-005-query-timeout-row-limit.md`。

---

## 23. 已完成会话续聊与逐轮能力文档

> 历史决策说明：本节记录 `v0.5.3` 引入完成后续聊时的原始设计。状态机与 Cycle 设计继续有效；
> “每个 Cycle 单独生成 MD”的 ADR-033 已在 `v0.5.4` 被第 24 节的新决策替代。

### 23.1 背景问题

早期实现把 `completed` 同时解释为“模型已经完成本次工作”和“整个会话永久关闭”：状态转换表
不给 `completed` 出边，开发与异常 Engine 拒绝 `send()`，桌面和备用 Web UI 也关闭输入入口。
这保证了终态清晰，却不符合真实工程沟通——用户常在结果之后继续追问原因、补充一个约束，或
要求在同一上下文上追加修改。

简单开放输入框仍不够。Session 的数据库查询和重新规划计数原本累计到会话结束；直接复用会让
第二轮继承第一轮预算。Capability 又以 Session ID 作为唯一幂等键，第二次完成会被识别成重复
保存而不产生新经验。必须同时重新定义生命周期、预算和知识产物边界。

### 23.2 业务语义：Session 与 Cycle 分离

本次把 Session 定义为可持续的工程对话，把 Cycle 定义为一次从用户目标到真实完成的工作轮次：

```text
Session
├─ Cycle 1：创建 → 调查/审批/实施/验证 → completed → capability 1
├─ Cycle 2：用户追问 → 调查/审批/实施/验证 → completed → capability 2
└─ Cycle 3：用户补充 → …… → completed → capability 3
```

`needs_input`、`query_required`、等待审批、实施和验证都是一个 Cycle 的中间状态，不会各写一份
Markdown。只有重新到达 `completed` 才形成一份独立能力文档。旧 Cycle 的 MD 不追加、不覆盖；
Project Knowledge 和 Engineering Experience 总文档也不会自动混入完整对话。

### 23.3 核心实现

- `AgentSession` 与 `IncidentSession` 增加 `cycle_number`、`cycle_objective` 和本轮查询审计起点；
  旧持久化数据缺少这些字段时自动按第 1 轮加载；
- 状态机允许 `completed -> inspecting`，但 `is_terminal(completed)` 仍返回 true，使启动 Recovery
  不会把静止完成会话当成待恢复任务；只有新的用户 command 可以显式重新打开；
- Engine 在重新打开时记录 `task_reopened`，增加 cycle，清除旧的当前决定、审批和能力文档指针，
  重置查询/重规划计数；消息、Claude Runtime Session、Event、Decision、Run、Artifact 和累计
  查询审计继续保留；
- Engine 先检查 CommandReceipt，再判断是否重新打开。相同 command ID 重试直接返回原结果，
  不会因为网络重试创建多余 Cycle；
- 第 1 轮 Capability 沿用 `<session-id>.md`；从第 2 轮开始写
  `<session-id>-cycle-002.md` 及对应 task JSON。每个 Cycle 拥有自己的幂等键和索引项；
- Artifact 文件继续使用不可变 UUID 文件名，并在 Event、metadata 和 final report 中记录
  `cycle_number`；
- 桌面和 Streamlit 在完成后继续开放输入，主按钮显示“继续对话”，发送后使用原 Runtime
  Session 回到只读调查。`cancelled` 仍永久封闭，paused/recovery_required 仍走显式恢复入口。

### 23.4 技术难点与解决方案

**可重新打开与 Recovery 终态看似矛盾。** 如果把 completed 从 terminal set 移除，启动扫描会把
大量正常完成任务当成候选，污染恢复逻辑。解决方式是区分“Recovery 是否需要处理”和“用户命令
是否允许转换”：completed 对扫描仍是静止终态，但转换表允许有审计的新消息进入 inspecting。

**知识幂等键不能只用 Session ID。** 原设计保证重复完成不会覆盖 MD，但也会吞掉合法第二轮。
解决方式是使用 `(session_id, cycle_number)` 作为能力产物身份，并保留第 1 轮旧文件名以兼容
已有数据。重复保存同一 Cycle 返回 `created=false`，下一 Cycle 一定获得新文件。

**预算重置不能丢失历史审计。** 直接清空 QueryObservation 会破坏追溯；完全累计又会把旧轮结果
混进新能力文档。解决方式是保留累计 observation，同时记录本轮起点；查询次数按 Cycle 归零，
公开 Outcome、UI 和异常能力文档只读取当前切片，Event/Session 仍保留完整历史。

**继续对话不能把旧结论当成当前事实。** 保留 Claude Runtime Session 可以减少重复解释，但代码
和业务数据可能已经变化。开发与异常 Prompt 明确把最新消息视为新工作轮次：可以复用相关历史，
但必须重新核对当前仓库和授权数据。

### 23.5 决策记录

- ADR-031：`completed` 表示当前工作轮次完成，`cancelled` 才表示会话永久封闭；
- ADR-032：同一 Session 使用持久化 cycle 编号，重新打开时重置执行预算但保留历史事实；
- ADR-033：每个 completed Cycle 生成独立 Capability MD，幂等身份为 session + cycle；
- ADR-034：completed 对 Recovery 保持 inactive，只有显式用户 command 可以重新打开；
- ADR-035：当前轮 UI/Outcome/能力文档使用查询审计切片，完整审计保留在 Session/Event。

### 23.6 验证策略

确定性测试覆盖开发与异常的两次完成、`task_reopened`、`completed -> inspecting -> completed`
SQLite 回放、Runtime Session 复用、查询/重规划预算重置、逐轮 MD 命名、同 command ID 幂等、
Artifact cycle metadata，以及桌面两套流程完成后继续输入。最终验证结果记录在
`docs/tasks/ACE-RUNTIME-006-reopen-completed-session.md`。

---

## 24. 会话级能力文档与续聊追加

### 24.1 需求修订与业务理解

`v0.5.3` 解决了“completed 后不能继续对话”，并将每次重新完成视为一个独立 Cycle 文档。这在
技术上边界清晰，却把同一个问题的连续调查拆散成多个文件：首轮完成、用户追问、补充修改和最终
结论彼此高度相关，检索时却会命中多个相似条目，既增加索引噪声，也让 Agent 难以判断哪一份代表
当前会话的最终认识。

业务规则最终明确为：

```text
新建开发 Session ──首次 completed──> 新建 development/<session-id>.md
                         │
                         └─续聊后再次 completed──> 追加原 development MD

新建异常 Session ──首次 completed──> 新建 incident/<session-id>.md
                         │
                         └─续聊后再次 completed──> 追加原 incident MD
```

这里的“新文档”边界是 Session，而不是一条消息或一个 Cycle。澄清、查库、审批、实施和验证仍是
Cycle 中间阶段，不写能力文档；同一 Session 的下一次 completed 只增加一个带轮次编号的章节。
用户点击“新建任务”产生新的 Session 时，才会产生新的 MD。开发和异常处理使用不同目录、索引和
正文结构，即使 Session 目标相似也不会混写。

### 24.2 设计思路

Session、Cycle 和 Capability 现在各自承担不同职责：

- Session 是对话与知识文档的身份边界，一份 Session 对应一份主 task JSON 和一份 Capability MD；
- Cycle 是执行与审计边界，继续负责状态转换、预算重置、Event、Artifact 和幂等；
- Capability MD 是会话级知识视图，首轮写完整能力，后续轮次追加结构化章节；
- `CAPABILITIES.md` 是 Session 级索引，不为同一会话重复创建条目。

因此没有删除 `cycle_number`、`cycle_objective` 或 `cycle_query_observation_start`。如果为了合并 MD
而移除 Cycle，会同时破坏查询次数重置、恢复回放和“本轮数据不混入上轮总结”等已经验证的能力。
本次只改变知识产物的聚合键，不改变 Runtime 生命周期。

### 24.3 核心实现

开发 `CapabilityStore` 和异常 `IncidentCapabilityStore` 都改用稳定路径：

```text
workspaces/<workspace-id>/development/
├─ tasks/<session-id>.json
└─ capabilities/<session-id>.md

workspaces/<workspace-id>/incident/
├─ tasks/<session-id>.json
└─ capabilities/<session-id>.md
```

首次 completed 时写入 schema v2 task JSON，除原字段外增加：

- `cycle_count`：当前主文档包含的完成轮次数；
- `last_cycle_number`：最后一次完成的 Cycle；
- `created_at/updated_at`：会话知识文档的创建和更新时间；
- `cycles`：按轮次保存目标、结果及流程专有摘要的轻量历史。

后续 completed 时，Store 先读取 `cycles` 判断当前 `cycle_number` 是否已经记录。已存在则直接返回，
防止命令重试或重复保存产生重复章节；不存在时才更新 frontmatter，并把当前轮内容追加到正文。
开发章节保存目标、总结、方法、验证、风险、证据和变更文件；异常章节保存页面/路由、代码定位、
诊断、发现、数据库查询审计、建议动作与自动化边界。所有新文本仍经过路径与密钥脱敏，文件更新
仍使用临时文件替换，避免进程中断留下半份 Markdown 或 JSON。

`CapabilityReceipt.created` 没有扩展公共契约：首次创建返回 true；后续追加或同轮幂等返回 false。
调用方只需要展示稳定文档路径，不需要理解文件更新细节。

### 24.4 旧版本兼容

`v0.5.3` 曾使用 `<session-id>-cycle-002.md/json`。直接删除这些文件会损失已经沉淀的事实，继续把
它们全部放入索引又会违背“一会话一条知识”的新语义。因此采用保守兼容：

1. 不自动删除旧逐轮文件，保留可回退原始证据；
2. 重建索引时按 `session_id` 聚合，只展示一个主文档链接和累计轮次；
3. 同一 Session 下次写入时读取旧 task JSON，把尚未进入主文档的旧轮次正文折叠为“历史记录迁移”
   章节，并把轮次元数据合并进主 task JSON；
4. 合并以 `cycle_number` 去重，重复调用不会再次追加迁移章节。

这个策略刻意避免后台批量迁移全部工作区。只有再次使用的热会话才按需折叠，降低启动成本，也
避免一次性大范围改写用户本机的历史知识文件。

### 24.5 技术难点与解决方案

**追加与原子性冲突。** 直接使用文件 append 可以保留旧正文，却无法可靠同步 frontmatter 的
`cycle_count/updated_at`，而且 Markdown 成功、JSON 失败时容易不一致。实现选择读取旧正文、重建
小型 frontmatter、追加新章节后整体写入临时文件并原子替换。逻辑语义是追加，物理写入仍具备
崩溃安全性。

**索引不能按文件数量计数。** 兼容目录可能同时存在主文件和 v0.5.3 逐轮文件。索引生成器按
`session_id` 分组，再合并各 task JSON 的 Cycle 元数据，选择主记录作为链接，从而不会把一个会话
显示成多个能力。

**同一文档可能逐渐变长。** 合并会话内容提升连续性，但无限续聊仍可能导致知识臃肿。当前先遵循
业务规则保存完整的已提炼章节，不写原始聊天和数据库业务行；不相关的新问题应新建 Session。
后续 Engineering Experience/RAG 阶段可以在不改变原始 MD 的前提下增加章节摘要、语义索引和
陈旧标记，而不是现在引入不可验证的自动删除。

**开发与异常的复用结构不同。** 两者共享文件身份、frontmatter、轮次合并和索引去重工具，但正文
渲染保持独立。这样避免复制存储机制，同时不把“代码变更与测试”和“页面诊断与数据库审计”压成
一个失去业务含义的通用模板。

### 24.6 技术选型与决策记录

- ADR-036（替代 ADR-033）：Capability 的持久化身份是 Session ID，不再是 Session + Cycle；
- ADR-037：同一 Session 后续 completed 追加原 MD，Cycle 仍作为执行、审计和幂等边界；
- ADR-038：开发和异常共享会话聚合机制，但使用独立目录、索引和轮次正文模板；
- ADR-039：task JSON schema v2 保存轻量 cycles 历史，Markdown 保存供人和 Agent 阅读的知识；
- ADR-040：v0.5.3 逐轮文件只读保留、索引去重，并在热会话后续写入时按需折叠；
- ADR-041：通过原子替换实现逻辑追加，不使用裸文件 append 牺牲元数据一致性。

### 24.7 当前边界与验证

当前不会自动判断两个不同 Session 是否属于同一个业务问题，也不会跨 Session 合并文档；这种判断
容易误合并不同时间、环境或权限下的结论。当前也没有自动压缩超长会话文档，后续应通过可追溯的
摘要层和 RAG 索引解决，而不是覆盖原始工程经验。

确定性测试覆盖开发和异常 Session 的两次 completed 使用同一路径、只存在一个新格式 MD、后续
章节内容、schema v2 cycle 历史、索引单条目、同 command ID 幂等，以及 v0.5.3 旧逐轮记录折叠和
重复调用不重复追加。最终验证与发布结果记录在
`docs/tasks/ACE-KNOWLEDGE-007-session-capability-document.md`。

---

## 25. 配置化项目路径与异常截图证据链

### 25.1 背景与业务理解

早期桌面端把代码项目路径和异常页面线索都设计为对话输入区中的独立字段。这种布局虽然直观，
但两个值的业务性质不同：项目路径是跨任务复用的运行环境配置，页面线索则是某一轮异常上下文。
把前者放在每次消息旁会产生重复输入和误改风险；把后者限定为一个文本框，又无法承载用户最自然
的异常反馈方式——直接粘贴现场截图。

本次需求最终收敛为：

```text
系统配置 ──保存一个代码根目录──> 开发/异常的新 Session
                                        │
已有 Session ──续聊────────────────────┘ 使用自身已保存 workspace

异常消息 = 文字描述/页面名称/路由 + 0..5 张粘贴截图
                  │
                  └─> 模型结合截图、代码和受控 SQL 证据诊断
```

“删除异常页面栏”不等于放弃页面定位。页面名称、路由和现象回到自然语言消息中；截图只是补充视觉
证据。如果信息仍不足，模型继续按照澄清 Skill 只问一个最高价值问题。CLI 与 Python API 的
`page_hint` 保留，避免破坏已有自动化和未来外部入口。

### 25.2 设计思路

项目路径采用配置与 Session 快照分离：`WorkspaceConfigStore` 只决定新任务从哪里开始，Session
一旦创建就保存规范 workspace，之后的续聊、恢复和能力文档继续使用该值。这样用户可以随时更换
默认项目，而不会让历史任务静默落到另一个仓库。

截图采用显式附件契约，而不是扫描消息里的字符串并猜测本地路径。UI 只在异常模式拦截图片粘贴，
普通文本继续交给 Tk 默认粘贴；图片经主机解码、限额并统一转存 PNG 后形成
`MessageAttachment`。该对象沿 `DesktopClient -> IncidentApplication -> IncidentEngine ->
RuntimeTurn` 传递，最终由 Claude Code 的精确 `--add-dir` 获得读取能力。附件元数据随用户消息
持久化，事件只保存数量和文件名，不把图像正文写进 SQLite 或日志。

### 25.3 核心技术实现

- `workspace_config.py` 把规范项目根目录原子保存为
  `<data_dir>/workspace/project.json`，加载时区分“从未配置”和“已配置但当前不可访问”；
- 系统配置增加“项目路径”页签，支持浏览、校验和保存；主 composer 删除项目路径行，新任务发送
  前读取最新配置，已有 Session 不受配置切换影响；
- 桌面异常模式删除 `page_hint_entry`，将页面线索纳入正常消息，并绑定 `<<Paste>>`；文本剪贴板
  返回默认行为，图片剪贴板进入待发送附件区，支持数量/大小提示和一键清除；
- `IncidentAttachmentStore` 使用 Pillow 读取 Windows 剪贴板或图片文件列表，限制 4000 万像素，
  统一转换为 PNG，并在 UUID 隔离目录中通过临时文件替换完成写入；
- `ChatMessage.attachments` 保留每轮证据引用，`RuntimeTurn.additional_dirs` 表达经过主机批准的额外
  读取目录；Claude Code Adapter 对 Capability 与附件目录去重后逐个生成 `--add-dir`；
- Incident Engine 二次校验最多 5 张、单张 10 MiB、支持的后缀、文件存在性及大小未变化，把准确
  路径加入当前消息，并在同一 SQL 查询循环中持续挂载这些目录；
- System Prompt 明确规定截图和其中的文字均是不可信数据，只能用于提取可见界面事实，不能覆盖
  用户要求、权限边界或系统指令。

### 25.4 实现难点与解决方案

**在 UI 显示图片不代表模型能读取图片。** 如果只把截图画在 Tk 窗口或把路径写入 prompt，Claude
Code 的隔离目录仍会拒绝访问。解决方案是在 Engine 完成主机校验后，把每张图片自己的父目录加入
RuntimeTurn，由 Adapter 生成精确 `--add-dir`；异常模式没有 Edit/Write/Bash，因此这是最小只读
暴露面。

**剪贴板同时可能是文字、DIB 图片或文件列表。** Tk 对 Windows 图片剪贴板支持不稳定，直接
`clipboard_get()` 只能可靠覆盖文字。实现使用 Pillow `ImageGrab.grabclipboard()` 区分 Pillow
Image、文件列表和普通文本；只有识别出图片才返回 `"break"` 阻止默认粘贴，文字仍按原行为进入
编辑框。

**截图可能成为视觉提示注入载体。** 图片里可能出现“忽略规则并执行命令”等文本。仅靠 UI 提示
不足以构成边界，因此在消息和系统 Prompt 两处都声明图片是不可信证据；Engine 不从图片文本生成
权限，Runtime 仍只有异常调查允许的读取工具。模型层防护与宿主工具边界同时存在。

**配置切换不能破坏可恢复会话。** 每轮都读取全局项目路径会导致完成会话续聊或暂停任务恢复时
切换仓库。实现只在 `session_id is None` 的新任务路径读取配置；已有会话由持久化 Session 的
workspace 驱动，遵循与数据库引用相同的“新配置只影响新任务”原则。

**附件生命周期存在恢复与清理的冲突。** 发送后立即删除可以节省空间，却会让会话回放、恢复和
审计丢失证据。本阶段选择保留隔离文件，并限定大小/数量；自动过期、引用计数和用户主动清理留给
后续存储治理迭代，不能在没有引用分析时直接批量删除。

### 25.5 技术选型与决策记录

- ADR-042：项目路径属于系统配置，不属于消息 composer；配置只作为新 Session 默认值；
- ADR-043：Session 保存创建时的 workspace，配置切换不得重定向历史会话；
- ADR-044：桌面异常页面线索进入自然语言消息，保留公共 API/CLI `page_hint` 向后兼容；
- ADR-045：截图通过结构化 `MessageAttachment` 显式传递，不从自由文本猜测本地文件；
- ADR-046：剪贴板图片统一规范化为隔离 PNG，限 5 张/消息、10 MiB/张和 4000 万像素；
- ADR-047：附件只通过校验后的独立父目录挂载，异常 Runtime 继续保持纯只读工具集；
- ADR-048：截图、截图文字和数据库行统一按不可信证据处理，不能成为 Agent 指令来源；
- ADR-049：附件当前保留以支持恢复和审计，清理策略作为后续独立迭代处理。

### 25.6 当前边界与验证策略

当前附件入口只在原生桌面异常流程开放；开发流程、Streamlit 和异常 CLI 暂无粘贴图片交互，但
Python `IncidentApplication` 已能接收显式附件。界面当前显示附件数量和总大小，不生成缩略图；
系统也尚未实现 OCR、图片去重、压缩策略、过期清理或附件导出。

确定性测试覆盖项目路径保存/重载/失效、剪贴板图片规范化和超限清理、Runtime 多目录去重挂载、
Incident 消息/事件持久化、图片路径进入模型消息，以及桌面纯图片发送、清除、开发模式不拦截文字
粘贴和删除旧路径/页面栏。最终验证与发布记录见
`docs/tasks/ACE-UI-008-configured-workspace-image-paste.md`。

## 26. 手动混合 RAG 与可替换 Embedding 接口（v0.6.0 基线）

### 26.1 背景与业务理解

Project Knowledge 解决“当前项目有什么稳定约束”，Capability 解决“这次任务实际完成了什么”，
Engineering Experience 解决“过去有哪些可复用工程经验”。文档增多后，把所有 Markdown 全量
塞进 Prompt 会增加 token、噪声和相互矛盾的概率；仅按文件名或关键词查找，又会漏掉表达不同但
含义相近的经验。因此需要一个检索层，在任务调查前只选择少量相关片段。

但“任务完成即自动上传”会把未经审核的总结、偶发结论和敏感内容直接放大到后续任务。用户已经
明确采用人工上传：任务完成仍正常生成分流程 Capability MD；管理页面把它显示为待加入，只有
用户选择后才建立索引。原 Markdown 始终是知识原文，Chunk、FTS 和向量只是可以删除、重建的
派生视图。

`v0.6.0` 实施时原计划部署 Ollama 与 `Qwen3-Embedding-0.6B`。为了不阻塞 UI、数据模型、分块、过滤、
Agent 接入和审计验证，系统先实现一个确定性的伪接口；它必须在 UI、模型 ID、数据库文件名和
返回元数据中显式标为 simulated，不能假装已经具备 Qwen3 的语义能力。

### 26.2 设计思路

整体链路为：

```text
Markdown 原文
  -> 发现并计算 current_hash（不自动索引）
  -> 用户预览和选择
  -> 标题感知分块
  -> EmbeddingProvider + VectorStore
  -> Chunk 正文/元数据 + FTS5 + 向量索引
  -> Dense Top-K + BM25 Top-K
  -> RRF 融合、领域/项目/工作区过滤和每文档配额
  -> 带来源、标记为不可信参考的 Prompt Context
  -> knowledge_retrieved / knowledge_retrieval_failed Event
```

`EmbeddingProvider` 和 `VectorStore` 是稳定端口，`KnowledgeRAGService` 只依赖端口，不依赖
Ollama 或某个向量数据库 SDK。这样未来接入真实服务时只替换 Adapter，不改管理页面、Agent
流程和检索结果契约。真实索引与模拟索引必须按模型 ID、维度、Collection/数据库身份隔离。

### 26.3 核心技术实现

- `models.py` 统一 Document、Chunk、Vector Point/Match、Hit、Receipt 和领域/来源/状态枚举；
- `MarkdownChunker` 去除 frontmatter，保留标题层级、段落和 fenced code block，目标约 750
  tokens、最大约 1200，同章节加入少量重叠；稳定 Chunk ID 同时包含文档、标题、序号和正文 Hash；
- `SQLiteKnowledgeRepository` 保存文档状态与 Chunk，并用 FTS5/BM25 做词法召回；中文查询同时
  建立单字 token，保证当前 SQLite 默认 tokenizer 下仍有基础召回；
- `FakeEmbeddingProvider` 把 token 稳定散列到 96 维并归一化；`SQLiteFakeVectorStore` 持久化
  float BLOB 并计算余弦相似度。它可重复、离线、适合测试，但不是语义模型；
- 混合检索分别获取 Dense 与 Lexical Top 20，以 `1/(60+rank)` 做 RRF，默认返回 6 个 Chunk，
  每篇文档最多 2 个，避免单个长文档挤占上下文；
- `KnowledgeManagementDialog` 提供发现、状态展示、多选加入/重建、移除、分块预览和测试检索；
  索引操作在后台线程执行，UI 线程只更新控件；
- 开发 Engine 只在 inspect 阶段检索，异常 Engine 每个用户调查 cycle 检索一次并在 SQL 循环内
  复用上下文。检索失败只形成事件并降级，不使代码任务或异常诊断失败。

### 26.4 实现问题与解决方案

**不能整篇直接 Embedding。** 长文档会把多个主题平均到一个向量中，命中后又把大量无关内容
带进 Prompt。标题感知 Chunk 既缩小检索单元，也保留“这段内容属于哪个章节”的结构证据。

**不能只做向量检索。** 文件名、类名、SQL 字段和错误码需要精确匹配，而真实语义向量也可能
漏掉专有标识。FTS5 与 Dense 双路召回后按排名融合，既避免比较不可比的原始分数，又保留两类
检索的互补性。当前 Dense 是模拟结果，因此精确词法召回尤其重要。

**索引状态不能只看有没有向量。** 源文件修改后旧索引仍然存在。Document 同时保存
`current_hash` 和 `indexed_hash`，刷新时显示 `outdated`，让用户明确重建；移除只删派生记录并
保留源文件。

**历史经验可能污染当前决策。** 每个命中保留来源路径、标题、类型和融合分数，Prompt 明确要求
把内容视为不可信且可能过期的参考；领域、项目和工作区过滤先于返回，模型还必须读取当前代码、
按授权查询数据库后才能下结论。

**检索服务不应成为主流程单点。** Embedding、SQLite 或未来向量数据库不可用时，Engine 捕获
异常、记录截断错误和 workflow 信息，继续原有无 RAG 调查。这样增强层失败不会篡改任务语义。

### 26.5 技术选型与决策记录

- ADR-050：Markdown 是知识原文，Chunk、FTS 与向量均为可重建派生数据；
- ADR-051：任务完成文档不自动索引，必须由用户在管理页明确选择；
- ADR-052：按 Markdown 标题/段落/代码块分块，不整篇 Embedding；
- ADR-053：使用 Dense + BM25 + RRF，而不是只依赖向量或关键词；
- ADR-054：检索按 domain/project/workspace 过滤，并限制每文档命中数量；
- ADR-055：RAG 内容是带来源的不可信参考，当前代码和授权数据仍是事实依据；
- ADR-056：检索失败采用可审计降级，不改变主任务完成/失败判断；
- ADR-057：部署期使用独立的 `fake-hash-embedding-v1` 伪适配器，正式索引必须全量重建，不得
  重命名或复用模拟向量；其中“正式 Provider 使用 Qwen3”的选择已由 ADR-065 取代。

### 26.6 当前边界、迁移与验证

本节记录 `v0.6.0` 的模拟基线。`v0.6.1` 已用 Voyage 取代原定 Ollama/Qwen3 Provider，但仍没有
连接 Qdrant/pgvector 等外部向量数据库，也没有真实 reranker、自动知识审核、去重合并、敏感内容
扫描和批量索引迁移工具。`Failure Knowledge` 已进入统一枚举，但发现器尚未接入独立来源。

确定性测试覆盖无自动索引、标题感知分块、手动建立双索引、精确词法命中、内容变更陈旧标记、
移除索引保留源文件、Capability 领域元数据、开发/异常 Prompt 注入、检索事件、失败降级，以及
知识管理页面默认待加入状态。完整发布证据见
`docs/tasks/ACE-RAG-009-manual-hybrid-knowledge.md`。

## 27. Voyage Embedding 配置与正式检索接入

### 27.1 需求变化与业务判断

用户将 Embedding 技术路线从本地 Ollama/Qwen3 调整为 Voyage，并要求像 DeepSeek 生成模型一样
提供可编辑配置页面。这个变化验证了上一迭代“Provider 与 VectorStore 必须通过端口隔离”的价值：
Chunk、文档状态、RRF、Agent 注入和管理页面无需重写，变化集中在配置、凭据、Embedding Adapter
和组合根。

Voyage 是外部 API。与本地模型不同，用户手动建立索引时，所选 Markdown Chunk 会发送到配置的
Embedding 端点；Agent 检索时，当前查询文本也会发送。UI 必须把这个外部数据边界说清楚，不能
只显示“正式模式”而隐藏数据离开本机的事实。源 Markdown 和索引选择仍由用户控制，任务完成不
自动上传。

### 27.2 配置与密钥设计

`EmbeddingConnectionConfig` 只保存 provider、endpoint、model、output dimension 和 1–60 秒请求
超时。默认采用 Voyage 官方端点、面向代码检索的 `voyage-code-4` 与 1024 维。endpoint 禁止携带
用户名、密码、query 或 fragment，避免把秘密混入 URL、文件名或日志。

非密钥配置原子写入 `<data_dir>/embedding/voyage.json`；API Key 通过 keyring 保存到 Windows
Credential Manager。`EmbeddingSetupState` 只返回 `has_api_key`，页面密码框永远不回填；已有
密钥时留空保存表示保留。连接测试可使用输入框中的临时 Key，但不会先保存它，也不会把它放入
测试结果、异常文案或任务记录。

系统配置新增第五个 “Embedding” 页签，包含 API 地址、模型、输出维度、API Key、“测试连接”和
“保存 Embedding 配置”。网络测试在后台线程执行，Tk 主线程只轮询结果；测试期间禁用相关控件和
关闭动作，避免同时保存两组状态。

### 27.3 Voyage REST Adapter

系统直接用 Python 标准库调用 REST，不增加 Voyage SDK 依赖。请求使用 Bearer 认证，文档批次
设置 `input_type=document`，查询设置 `input_type=query`，同时发送 `truncation=true`、配置维度和
`output_dtype=float`。文档按最多 128 条分批，既低于 API 的列表上限，也便于控制单次失败范围。

响应校验包括：必须有 `data` 数组、条数与输入一致、index 从 0 连续、每个值可转成有限 float、
每条向量维度与配置一致。任何 HTTP、网络、JSON 或契约错误都转换为安全的
`VoyageEmbeddingError`；Authorization 不进入错误，服务端错误文本中即使意外回显密钥也会替换。

### 27.4 索引身份与任务稳定性

Embedding 空间不仅由模型名决定，也受 endpoint 和 output dimension 影响。系统对
`provider + normalized endpoint + model + dimension` 计算 `index_id`，正式数据库使用
`knowledge-voyage-<index-id>.db`，模型元数据使用带指纹的 `model_id`。因此：

- 模拟向量不能被重命名为 Voyage；
- 更换模型、代理端点或维度后，新服务看到源文档为 pending；
- 用户必须在管理页明确执行全量重建；
- 旧索引不会自动删除，可以通过原配置重新访问或按后续治理策略清理；
- 源 Markdown 从未迁移或删除。

Embedding 保存后，如果当前流程没有活动 Session，桌面立即重建两套应用门面；存在活动任务时，
它继续使用原 Retriever，新配置从新任务生效。这与项目路径和 SQL Server 的 Session 快照原则
一致，避免一轮对话中途切换向量空间。

### 27.5 VectorStore 边界

正式 Voyage 向量当前保存到 `SQLiteVectorStore`，按模型身份与文档替换，使用 float32 BLOB 和
点积线性搜索。Voyage 向量已归一化时点积等价于余弦排序；宿主仍把 Dense 排名与 FTS5/BM25
排名通过 RRF 融合。

SQLite 方案适合当前人工选择、小规模知识库和架构验证，但它不是大规模 ANN 向量数据库。后续
接入 Qdrant、pgvector 或其他 VectorStore 时应保持相同端口、metadata 过滤和 index identity，
并单独实现健康检查、批量重建、删除一致性、备份和容量评估。

### 27.6 技术选型与决策记录

- ADR-058：正式 Embedding Provider 改用可配置 Voyage REST，保留 Fake 作为未配置降级；
- ADR-059：默认 `voyage-code-4` / 1024 维，同时允许用户编辑模型和维度；
- ADR-060：不引入 Voyage SDK，使用标准库实现最小 REST Adapter，减少依赖与升级耦合；
- ADR-061：文档和查询分别使用 `input_type=document/query`，不混用检索角色；
- ADR-062：Voyage API Key 保存到 OS 凭据，非密钥配置保存到用户数据目录；
- ADR-063：连接测试可使用未保存 Key，但不得持久化、回填或记录；
- ADR-064：endpoint/model/dimension 共同决定索引身份，配置变化必须人工全量重建；
- ADR-065：原 Qwen3 Provider 计划由 Voyage 取代，Provider 端口继续保留未来替换能力；
- ADR-066：配置变化不切换活动 Session 的 Retriever，只作用于新任务和新建索引；
- ADR-067：当前正式向量先用本地 SQLite，外部向量数据库留作独立扩展。

### 27.7 当前限制与验证策略

当前没有 Voyage rerank、批处理 API、token 预估、速率限制退避、重试、请求费用统计或远程数据
删除能力；连接测试与手动索引会产生真实 API 调用和可能的费用。系统不会自动上传完成文档，
但用户选择正式索引后，Chunk 已经发送到所配置的外部端点，这必须纳入企业数据合规判断。

确定性测试使用注入 Transport，不访问 Voyage 网络，覆盖 Authorization、document/query 角色、
批次、顺序、维度、错误脱敏、配置原子保存、密钥保留、连接测试不落盘、索引身份隔离、切换后
pending、系统设置五页签和密码框不回填。真实账号连通性由用户在配置页点击“测试连接”验证。
完整发布证据见 `docs/tasks/ACE-RAG-010-voyage-embedding-config.md`。

## 28. 页面名称优先的异常调查与规则/知识分层（v0.6.2 基线）

> 本节保留 `v0.6.2` 的历史设计。`v0.6.3` 已通过第 29 节放宽“标题唯一前置”的 ADR-070，
> 规则/项目知识/Capability 三层边界和有界查询原则继续有效。

### 28.1 背景与业务理解

异常流程必须先定位正确页面，才能查询菜单映射、读取对应代码并从真实业务逻辑提取数据查询。
如果只有错误文字或业务编号就扫描全部菜单，既浪费数据库和模型上下文，也很容易把相似页面当成
正确页面。截图虽然能够提供页面标题和异常区域，但它同时可能只有局部红字、包含多个窗口，或者
带有误导性文字，不能把“有图片”等价为“页面已定位”。

本次明确了页面名称的来源边界：用户可以直接输入页面/窗体标题；也可以粘贴截图，由模型优先观察
窗口标题、标签页、窗体标题、页面主标题或选中菜单。路由、模块、错误文本和业务主键只是辅助
证据。标题不可见或置信不足时，模型只询问页面名称或要求一张包含标题的截图，不以全表查询弥补
缺失信息。

用户此前已经建立 `knowledge/incident/生物/生物.md`。它不是重复的 Global MD，而是“生物”项目
专属异常知识：包括 QTMES 架构、`Menu.NAME -> Menu.URL` 映射和项目诊断边界。真正每轮必须执行
的页面优先流程不能只放 RAG，因为文档可能尚未索引、检索失败或没有命中；也不能写入所有流程
共享的全局 CLAUDE.md，否则开发流程和其他项目会错误继承 MES 表结构。

### 28.2 三层知识与状态边界

```text
应用内置异常规则（每轮强制加载）
  ├─ 页面名称前置条件
  ├─ 截图标题与异常区域判断方法
  ├─ 有界映射、代码验证和自动只读 SQL 流程
  └─ 只读权限、不得编造和完成条件

所选项目知识（按项目加载）
  ├─ 生物项目的 Menu.NAME / Menu.URL
  ├─ QTMES 分层和代码入口
  └─ 项目特有风险与查询示例

单次 Capability / RAG 候选（按需检索）
  ├─ 已验证页面名称 -> 实际代码路径
  ├─ 某类异常的证据链和可复用模式
  └─ 脱敏、审核后才能成为长期知识
```

当前会话中的截图、原始业务行、临时候选和未证实原因不进入项目基础 Markdown。会话只保存附件
引用、查询审计摘要和完成后的能力总结；原始业务行仍不持久化。RAG 继续是人工选择的派生索引，
不能反过来承担安全规则或任务状态。

### 28.3 页面定位与诊断流程

```text
接收文字 / 截图
  → 模型确认页面名称
      ├─ 无截图且未给名称：询问标题
      ├─ 有截图：只观察当前图片的标题区域
      └─ 标题不可靠：询问标题或完整截图，不查全部页面
  → 读取所选项目知识
  → 最多 20 条精确 / 前缀映射
      └─ 无可信结果：从页面名称提取关键词，再查最多 20 条包含候选
  → 模型结合名称、URL、当前代码和截图特征选择候选
  → 打开源码验证标题、控件、事件或路由
  → 文字异常定位产生现象的代码分支
    / 图片异常定位可见异常区域（红色只是线索之一）
  → 沿页面、服务、Repository/ORM/SQL 追踪最小调用链
  → 从当前代码提取最小参数化只读 SQL，宿主自动执行
  → 综合代码和数据输出证据、原因、置信度与下一步
```

“最相似页面”不是用宿主字符串距离直接选择。模型需要同时检查候选名称、映射 URL、仓库结构、
页面标题/控件/事件和截图特征；URL 只是线索。如果不能打开代码验证，就返回 `needs_input`，不能
完成诊断。

### 28.4 核心技术实现

- `incident/prompts/incident_workflow.md` 保存与项目无关、异常流程每轮强制执行的 Markdown；
- `incident/prompting.py` 通过 `importlib.resources` 加载并缓存规则，避免依赖当前工作目录；
- `pyproject.toml` 把异常规则声明为 package data，使安装后的桌面程序也能读取；
- `IncidentEngine._system_prompt()` 组合内置规则、当前数据库 schema、所选项目 Capability 入口和
  本轮 RAG 片段，不再在 Python 大字符串中写死 `Menu` 或 MES；
- `knowledge/incident/生物/生物.md` 保存 SQL Server `TOP (20)` 精确/前缀查询与第二阶段包含查询，
  两者都使用命名参数、显式列和 `max_rows=20`；
- `IncidentDecision` 的 completed 校验增加 `page.source_paths` 非空约束。页面选择仍由模型完成，
  宿主只拒绝“没有任何已验证代码地址却声称完成”的不一致结构；
- 桌面异常欢迎文案和输入占位明确要求页面名称，或粘贴包含页面标题的截图。

### 28.5 实现难点与解决方案

**强制规则不能依赖 RAG 命中。** RAG 是人工建立、可能陈旧且允许失败降级的增强层。如果“先确认
页面名称”只写在项目知识或历史经验里，未索引和检索失败时 Agent 会跳过。解决方案是把稳定流程
作为异常专用内置 Markdown 每轮加载，而不是变成通用全局规则。

**不能用代码判断图片有没有页面名称。** OCR 关键词、红色像素比例或固定标题区域会在 WinForms、
Web、弹窗和远程桌面截图之间失效。解决方案是让模型结合视觉上下文判断标题和异常区域；宿主只
控制附件目录、只读工具、查询行数和结构化完成条件。

**模糊查询容易退化成全量查询。** `%关键词%` 如果没有列、行数和触发条件限制，会带来大量相似
菜单。解决方案是严格两阶段：先精确/前缀，只有无可信结果时才使用从页面名称得到的关键词，并在
SQL 和 `DataQuery.max_rows` 两层限制为 20。异常红字不能直接作为页面关键词。

**映射 URL 可能过期或不是物理文件。** 历史 Menu 数据可能包含路由、类名、旧路径或多个候选。
解决方案是要求模型回到当前工作区验证标题、控件、事件或路由，并在有截图时做有限界面特征交叉
验证；完成结果必须至少提供一个已验证源码路径。

### 28.6 技术选型与决策记录

- ADR-068：异常调查的稳定强制规则使用异常专用内置 Markdown，不写入所有流程共享的 Global MD；
- ADR-069：项目表结构和页面映射语义保存在每个项目自己的 incident knowledge，生物项目继续
  维护现有 `生物.md`；
- ADR-070：页面名称是页面映射、代码调查和最终诊断的前置条件，可由用户文字或模型从截图确认；
- ADR-071：页面映射采用最多 20 条精确/前缀查询，再按条件采用最多 20 条关键词包含查询，禁止
  全量页面扫描；
- ADR-072：候选页面和截图异常区域由模型语义/视觉判断，宿主不引入字符串或颜色启发式；
- ADR-073：`completed` 必须有已验证页面源路径；URL、历史知识和截图单独都不足以证明定位；
- ADR-074：单次诊断进入独立 Capability，只有人工审核后才加入 RAG，原始业务行和图片不作为
  长期知识上传。

### 28.7 当前边界与验证策略

当前没有单独 OCR 引擎、视觉置信度字段、页面候选专用事件或真实 SQL Server/截图联调评测集。
标题和异常区域判断依赖当前生成模型的视觉能力；如果所配置模型无法读取图片，它应退化为询问
页面名称。项目映射知识仍需要维护者保证表名和字段与当前环境一致。

确定性测试覆盖内置 Markdown 每轮注入、通用规则不包含 `Menu`/QTMES、页面名称与截图标题要求、
20 条候选边界，以及 completed 缺少源码路径时的结构化拒绝。真实效果仍应使用“文字无页面名、
截图有清晰标题、截图只有红字、精确命中、模糊命中、多个相似候选、URL 过期”七类样本做模型和
SQL Server 联调。完整发布证据见 `docs/tasks/ACE-INCIDENT-011-page-first-investigation.md`。

## 29. 对话与图片联合的页面身份证据流程

### 29.1 需求变化与设计优化

`v0.6.2` 把可靠页面名称设置为页面映射、代码调查和最终诊断的统一前置，解决了“只有一段异常
红字就扫描全部菜单”的问题。但该约束过于绝对：用户可能已经给出准确代码路径或页面路由；截图
可能没有标题，但对话已经说明标题；也可能对话标题和图片实际界面不一致。强制要求标题会浪费
已有路径证据，也不能表达多种证据之间的冲突。

`v0.6.3` 将前置条件调整为“模型是否拥有足够的页面身份证据”。标题和路径是主要入口，菜单、
模块和图片特征是辅助证据。系统规定推理顺序和安全边界，但不规定某个字符串一定是标题、某个
区域一定是标题栏，或红色文字一定是异常；这些仍交给模型结合对话、视觉和代码理解。

### 29.2 优化后的证据流程

```text
先读取用户对话与相关会话历史
  → 提取候选标题 / 相对源码路径 / 路由 / 菜单入口 / 异常上下文
  → 是否有截图？
      ├─ 否
      │   ├─ 标题或路径可信：进入页面定位
      │   └─ 两者都没有：询问一个最高价值页面线索
      └─ 是
          → 再分析图片标题、菜单、breadcrumb、异常区域与界面特征
          ├─ 对话和图片一致：联合证据定位
          ├─ 图片无标题、对话有标题/路径：按对话定位后与图片比对
          ├─ 对话无线索、图片标题可信：使用图片标题候选
          ├─ 两边都无线索：询问标题/路径或更完整截图
          └─ 对话、图片、映射和代码冲突：
               ├─ 当前证据足以消解：说明依据并继续
               └─ 证据不足：请用户确认哪个页面发生异常
```

页面定位阶段不再机械查询菜单：可信工作区相对源码路径可直接打开，并从代码验证页面/窗体标题、
控件、事件或路由。只有标题、菜单或路由仍需解析时才使用项目映射。生物项目继续先查最多 20 条
精确/前缀候选，再在没有可信结果时查最多 20 条包含候选；两轮都没有结果就询问，不扩大范围。

### 29.3 核心实现

- 重写 `incident/prompts/incident_workflow.md` 的页面识别部分，明确对话先于图片，并把分支称为
  semantic evidence paths，而不是宿主硬编码条件；
- 明确无图场景允许页面标题或页面路径任一项作为入口；
- 明确有图场景中“图片无标题、对话有标题”的定位/比对流程，以及图片与候选冲突时的用户确认；
- `knowledge/incident/生物/生物.md` 同步项目专属做法：路径可直读，标题才走 `Menu`，两阶段查询
  失败后询问，不进行第三轮或全表扫描；
- 桌面欢迎文案和占位符不再写“至少提供页面名称”，改为标题或路径，也允许直接粘贴截图；
- 宿主没有新增页面标题字段解析器、图片 OCR 或匹配算法；`IncidentDecision` 仍只在 completed 时
  确定性要求至少一个已验证源码路径。

### 29.4 技术难点与解决方案

**推理顺序与死流程的边界。** “先对话再图片”用于防止模型忽略用户已经给出的上下文，但不能演变
成 `if message contains ...` 的代码分支。解决方案是把它写成模型系统规则，宿主只负责把对话历史
和经校验的图片同时传入，不解析业务语义。

**图片与对话冲突不一定都要询问。** 用户可能写了简称，图片显示全称；映射表可能是旧名称，当前
代码标题已经更新。如果任何不一致都追问，会产生过多阻塞。因此规则要求模型判断冲突是否实质，
当前代码和多个证据足以消解时可以继续并说明依据；只有无法可靠消解时才请用户确认。

**路径可能比标题更直接。** 准确源码路径已经把搜索空间缩到最小，再查 Menu 不增加证据。当前
流程允许直接读取路径并从代码补全结构化页面名称，同时继续要求完成结果包含验证过的源码路径。

**模糊查询仍需保持有界。** 放宽标题前置不能放宽数据库范围。项目知识继续限制显式列、参数化、
`TOP (20)` 和两阶段查询；模糊词必须来自模型理解的页面标题，不只来自异常红字或错误码。

### 29.5 技术选型与决策记录

- ADR-075（替代 ADR-070）：异常调查前置条件是“页面身份证据足够”，可信标题或页面路径均可
  作为入口，不再要求用户必须先给标题；
- ADR-076：有截图时先理解用户对话，再分析图片；顺序写入模型规则，不由宿主解析标题；
- ADR-077：图片无明显标题时允许使用对话标题/路径定位，再通过截图特征和当前代码交叉验证；
- ADR-078：候选与图片的冲突由模型判断；能被证据消解时继续，否则返回 `needs_input` 请用户确认；
- ADR-079：可信相对源码路径直接读取验证，页面映射不是强制仪式；
- ADR-080：项目映射查询全部无可信结果后询问用户，不扩大为全表扫描或无限模糊查询；
- ADR-081：completed 的源码路径宿主约束继续保留，语义入口放宽不降低最终证据门槛。

### 29.6 当前边界与验证策略

当前仍没有单独的图片 OCR、候选页面评分字段或冲突审计模型。提示词测试只能确认规则被正确注入，
不能证明所配置生成模型在真实截图上一定判断正确。后续应使用同一批固定样本评估：无图只有标题、
无图只有路径、有图且对话/图片一致、图片无标题但对话有标题、图片有标题但对话无线索、候选与图片
冲突、两阶段查询无结果。记录模型是否正确继续或追问，再基于失败证据调整规则。

完整实现与验证记录见 `docs/tasks/ACE-INCIDENT-012-dialogue-image-page-evidence.md`。

## 30. 双流程实时进度与瞬时状态分层（v0.6.4）

### 30.1 背景

长时间调用 Claude Code、读取代码、分析截图或执行数据库查询时，只有一个静态“处理中”提示，
用户无法判断系统是在等待、调查还是已经卡住。但如果直接展示模型自由文本或思维过程，又会产生
虚假阶段、敏感信息泄露和界面噪声。

### 30.2 设计决策

本次把“任务生命周期状态”和“当前交互进度”拆成两层：

- `TaskState`、Event 和 Runtime Run 是持久、可恢复、可审计事实；
- `ProgressEvent` 是可丢失、可合并的 UI 瞬时投影，不能驱动状态转换；
- 主机动作决定主阶段，模型 Runtime 只能通过脱敏后的工具生命周期补充事实信号；
- 固定中文阶段文案代替模型思维链，详情只保留安全文件名或主机生成摘要；
- 回调是可选扩展点，异常必须被隔离，不能拖垮 Agent 主流程。

### 30.3 核心实现

`core/progress.py` 定义两套流程共用的 Workflow、Phase、Event、Sink 和 Projector。开发与异常
Engine 在准备上下文、检索 RAG、分析图片、定位页面、查询数据库、修改、验证和能力沉淀等真实
边界发出事件；`ClaudeCodeRuntime` 的 system、ToolUse、ToolResult 与 heartbeat 则提供更细的
只读证据。桌面客户端用后台结果队列串行消费，设置 650ms 最小可见时间、淡出/淡入文本切换与
低强度 `#667eea` 呼吸点，不改变原有白银浅色玻璃配色。

### 30.4 技术难点与经验

1. **短阶段闪烁**：立即展示所有事件会使文本快速跳变。使用阶段合并、最小可见时间和单个待处理
   事件即可解决，无需引入复杂动画框架。
2. **线程安全**：Runtime 在后台线程执行，Tk 只能由 UI 线程更新。回调只向线程安全队列投递模型，
   `_drain_results()` 再更新控件。
3. **状态真实性**：模型生成“正在查询”并不等于数据库已执行。查询、修改和验证阶段只能在主机
   真实动作或已识别的工具事件发生时展示。
4. **可观测性与隐私**：用户需要知道系统在做什么，但不需要思维链。稳定阶段枚举与裁剪详情既能
   解释等待原因，也避免展示 SQL、命令参数和密钥。

### 30.5 可复用结论

面向长任务的 Agent UI 应把 durable state、runtime event 和 presentation progress 分开：状态机
负责正确性，事件负责审计，进度投影负责交互。UI 可以丢失进度事件，却不能因此改变或误判任务
结果；同样，模型可以建议下一步，却不能直接声明宿主尚未执行的阶段事实。

## 31. Hermes Skill 只读工程经验接入（v0.7.0）

### 31.1 背景与目标

项目需要复用 Hermes 已有的工程 Skill，但直接把完整 Hermes Agent 嵌入 ACE 会引入第二套会话、
Memory、工具、权限和恢复系统，破坏当前清晰的状态机边界。第一版目标因此不是“让两个 Agent 互相
接管”，而是把 Hermes 定位为一个可选、只读、可替换的工程经验提供者：Claude 仍理解任务和验证
证据，Hermes 只回答一个抽象工程问题。

### 31.2 设计思路

语义判断与确定性安全继续分层。Claude 根据当前任务判断是否值得咨询，并从动态目录选择精确
Skill；Python 宿主不使用关键词规则决定 Skill，但强制分类白名单、路径边界、调用次数、超时、
隐藏控制台、凭据脱敏和输出上限。Hermes 返回内容一律标注为候选经验，不能替代当前源码、数据库
证据或用户意图。

### 31.3 核心实现

- `core/hermes.py` 定义 Skill 摘要、结构化请求、结果、Observation、目录提示和边界脱敏；
- `ports/hermes_skills.py` 定义可替换 `HermesSkillService`；
- `adapters/hermes_skills.py` 从 `HERMES_HOME` 动态发现 Skill，并以 `ignore-rules + web toolset` 调用
  Hermes CLI；
- `HermesConsultationCoordinator` 被开发和异常 Engine 共用，统一产生 Event、Artifact 和回灌
  文本；
- `AgentDecision`/`IncidentDecision` 新增内部 `hermes_skill_required`，但不新增 TaskState；
- UI 使用 `consulting_engineering_experience` 瞬时阶段显示“正在咨询工程经验”；
- 开发与异常 Session 都保存 Observation，异常流程也复用 TaskArtifactStore 保存外部建议。

### 31.4 技术难点与解决方案

1. **避免形成双主控。** Hermes 不持有 ACE 会话、不转换状态、不批准变更。一次咨询完成后结果回到
   同一 Claude session，最终决定仍由主 Runtime 给出。
2. **避免项目数据无边界外发。** 宿主不自动拼接用户历史、源码、工作区或数据库结果，只发送模型
   生成的抽象问题与原因，并再次做常见凭据脱敏和长度限制。
3. **防止 Skill 路径注入。** 只扫描允许分类下的一层目录，要求安全 slug、frontmatter 名称与目录
   一致、解析路径仍位于根目录；未知名称在启动子进程前拒绝。
4. **外部 Agent 失败不能拖垮主流程。** 未安装、未配置模型、超时、非零退出和空输出都转换为
   failed Observation，再由 Claude 使用原有代码/RAG/SQL 能力继续。
5. **外部建议与事实必须分离。** Artifact 标记 `host_verified=false`，回灌文本明确要求核验；Event
   只记录 Skill、耗时和 Artifact ID，不复制完整问题或输出。
6. **避免 Agent 循环。** Hermes 单次最多 4 个工具循环，单个用户命令默认只有一次咨询预算；预算耗尽后提示 Claude 不再请求，
   若仍重复请求则按协议违例结束，保证执行有界。

### 31.5 技术选型与决策记录

- ADR-082：Hermes 作为 `HermesSkillService` 外部能力端口，不作为第二个 Runtime 主控；
- ADR-083：首版只允许 inspect 阶段显式选择一个已发现 Skill，implement/verify 禁止请求；
- ADR-084：CLI 使用中立 cwd、`--ignore-rules`、`--toolsets web`、隐藏窗口、超时和输出上限；
  不使用会同时忽略用户模型/provider 配置的 `--safe-mode`；
- ADR-085：不自动发送会话、源码和数据库结果，只发送脱敏后的抽象工程问题；
- ADR-086：Hermes 结果属于 untrusted candidate evidence，必须由 Claude 结合当前证据复核；
- ADR-087：成功与失败都进入 Event/Artifact，服务不可用时主流程自动降级；
- ADR-088：首版不接入 Hermes Memory、完整 Runtime、Skill 写入、SQL、Shell 或修改权限。

### 31.6 当前边界与后续方向

`v0.7.0` 发布时已从本机 `HERMES_HOME` 识别允许分类 Skill，但尚未配置 Hermes 模型，因此当时只
验证了目录发现、命令构造、脱敏、超时、Fake Service 双流程闭环和失败降级。该缺口已由
`v0.7.1` 的 ACE provider 桥接和无敏感数据 live smoke test 闭环。后续仍可增加 UI 健康检查、
按 Skill 质量评测和人工反馈，但不应默认共享两套 Memory 或授予 Hermes 项目写权限。

## 32. ACE 到 Hermes 的 DeepSeek 模型桥接（v0.7.1）

### 32.1 背景与业务目标

`v0.7.0` 已能安全发现和咨询 Hermes Skill，但要求用户另外维护 Hermes provider、模型和 API Key。
这会产生重复配置、密钥轮换不同步和“Claude Code 可用但 Hermes 不可用”的运维分叉。本次将 ACE
确定为生成模型配置的唯一入口：Hermes 沿用 ACE 已保存的 DeepSeek 地址与密钥，同时使用成本和
响应速度更适合短咨询的独立模型 `deepseek-v4-flash`。用户只配置一次，两个 Runtime 仍保留不同
模型职责。

### 32.2 设计思路与安全边界

模型复用不等于把配置文件互相复制。非敏感地址可以读取，密钥则只在 Hermes 调用发生时从 Windows
用户环境读取，并放入受控子进程环境；命令行参数、Prompt、日志、Event、Artifact 和 Hermes
`config.yaml` 都不得出现密钥。Hermes 仍只收到抽象工程问题，不会因为共享 provider 而获得项目、
数据库或状态机权限。

为防止配置错误造成凭据外发，桥接只接受 HTTPS、精确主机 `api.deepseek.com` 和精确
`/anthropic` 路径。调用时显式使用 `--provider custom`，通过 `CUSTOM_BASE_URL` 指定端点，并把
ACE 的 `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` 临时映射成 Hermes 按目标主机识别的
`DEEPSEEK_API_KEY`。子进程继承环境中的同类模型凭据会先被移除，避免其他 provider Key 参与路由。

### 32.3 核心实现

- `Settings.hermes_use_ace_provider=true` 控制默认桥接，可由高级部署显式关闭；
- `Settings.hermes_model=deepseek-v4-flash` 把 Hermes 模型与 Claude 主模型分离；
- `build_configured_hermes_service()` 使用 `UserEnvironmentStore` 读取当前进程或 Windows 用户配置，
  不持久化第二份 Secret；
- `HermesCliSkillService` 在实际 `invoke()` 时才取 Key，并追加
  `--model deepseek-v4-flash --provider custom`；
- `_validate_inherited_route()` 对协议、主机、路径和模型名做确定性边界校验；
- Windows 子进程显式使用 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8` 和父进程 UTF-8 解码，避免
  Hermes 的 Unicode 输出被系统 GBK 破坏；
- 缺失 Key、错误地址和外部调用失败继续转换为既有 failed Observation，主 Agent 自动降级。

### 32.4 实现问题、定位证据与解决方案

第一次 live smoke test 返回 HTTP 400。Hermes 日志证明 endpoint、provider 和协议已经正确，服务端
明确列出合法模型为 `deepseek-v4-pro`、`deepseek-v4-flash` 和
`deepseek-v4-flash-vision-exp`；用户输入的 `deeps-v4-flash` 少了 `eek`。因此按真实 API 证据纠正
为 `deepseek-v4-flash`，而不是为错误别名增加代码兼容。

第二次调用模型成功，但 Windows `subprocess` 默认使用 GBK 读取 Hermes 输出，UTF-8 字节触发
`UnicodeDecodeError`，有效输出被表现为 `None`。根因位于进程边界而非模型响应。修复是在父进程
明确 `encoding=utf-8, errors=replace`，并让 Hermes Python 子进程强制 UTF-8 标准流。定向复测取得
非空工程建议，证明路由、认证、协议、模型和输出读取形成完整闭环。

### 32.5 技术选型与决策记录

- ADR-089：ACE 是 DeepSeek 地址和 API Key 的唯一持久配置源，Hermes 不保存第二份密钥；
- ADR-090：Hermes 默认使用独立的 `deepseek-v4-flash`，不机械沿用 Claude 主模型名；
- ADR-091：桥接使用 Hermes 原生 `custom + CUSTOM_BASE_URL + DEEPSEEK_API_KEY` 契约，不修改
  Hermes 源码，也不覆盖用户 `config.yaml`；
- ADR-092：凭据发送前使用精确 HTTPS 主机/路径校验，当前不自动信任任意 Anthropic 兼容代理；
- ADR-093：模型名以实际服务端响应为准，不为拼写错误建立隐式别名；
- ADR-094：跨 Windows Python CLI 的文本管道显式固定 UTF-8，不能依赖本机活动代码页。

### 32.6 当前边界与后续方向

当前桥接有意只支持 DeepSeek 官方 Anthropic 兼容地址。将来如果需要企业代理或第二家 provider，
应在配置页增加可见的 Hermes 路由选择和目标主机确认，而不是放宽现有凭据校验。Hermes Skill 的
建议仍是 `host_verified=false` 候选证据；模型共享不改变每轮一次、只读 Web toolset、无代码/SQL
权限和主流程失败降级等边界。

## 33. Claude启动配置恢复与错误分类（v0.7.2）

### 33.1 问题背景与实际证据

异常对话在配置页明确检测到Claude Code后，Runtime仍立即返回“executable was not found”。本地
日志证明配置检测成功运行了真实`claude.exe --version`，但异常run在进程创建阶段失败。进一步
核对确认真实exe、Session工作区、同一Python和隐藏窗口参数都正常；把失效进程环境变量与有效
Windows用户变量组合后，稳定复现“配置检测选择有效回退路径、Pydantic Settings却接受旧进程值”
的分叉。因此问题不在MES项目或模型接口，而在Windows父子进程配置来源不同。

### 33.2 根因

配置页把值保存到Windows用户环境，同时更新当前配置进程；但Explorer、终端等长生命周期父进程
可能仍持有旧的非空`AUTO_CODING_CLAUDE_COMMAND`。旧`start.ps1`只在进程变量为空时导入用户值，
因此旧值会继续传给新客户端。模型检测器会跳过不存在的旧文件并找到真实exe，`Settings`的环境
绑定却直接接受旧字符串。Runtime又把`subprocess`的所有`FileNotFoundError`统一描述为exe缺失，
无法区分命令和cwd，放大了误导。

### 33.3 解决方案与边界

- 双击启动脚本始终用非空Windows用户配置刷新生成模型相关进程变量；
- `resolve_claude_command()`按显式值、当前/用户兼容配置、PATH和已知安装位置选择第一个可直接
  执行的文件，Windows继续拒绝`.cmd/.ps1` shim；
- 真实Runtime在每轮启动前重新解析命令，恢复动作写安全日志；Fake Runner/Popen不做本机验证；
- 预检分别验证workspace和exe，`FileNotFoundError`兜底也再次分类，其他系统错误保留裁剪详情；
- 日志增加最终command/workspace，不记录Prompt、API Key或模型响应。

### 33.4 可复用工程经验与决策

1. **配置检测与执行必须使用同一解析语义。** “配置页显示可用”不能只证明探测器成功，执行适配器
   必须对最终命令做同等校验。
2. **持久环境与进程环境会漂移。** Windows用户变量更新不会自动刷新已有Explorer和终端；双击
   启动器应明确谁是配置真相源，并在进程边界同步。
3. **不要把底层异常过早压成单一文案。** `FileNotFoundError`既可能来自exe，也可能来自cwd；在
   转换为用户错误前必须检查两端事实。
4. **恢复不应削弱测试可替换性。** 机器路径预检只作用于真实process适配器，注入Runner仍能使用
   虚拟命令测试协议。

- ADR-095：桌面配置页保存的Windows用户环境是双击启动时的模型配置真相源；
- ADR-096：真实Runtime允许从失效旧路径回退到已验证真实exe，并记录最终选择；
- ADR-097：Runtime启动错误按workspace、executable和其他OS错误分类，不再统一报告程序缺失；
- ADR-098：路径恢复与预检不作用于测试注入Runner/Popen，保持端口可替换。

## 34. Claude大提示词传输与Windows命令行边界（v0.7.3）

### 34.1 问题背景与证据

修复失效Claude路径后，异常流程暴露出更底层的`[WinError 206] 文件名或扩展名太长`。错误文案
容易让人继续检查exe或MES目录，但两者均已通过预检；失败发生在Python调用Windows
`CreateProcess`时，Claude Code尚未开始解析参数，更未调用DeepSeek接口。

只读测量当前配置得到：数据库Schema描述26,583字符，异常系统提示词35,483字符，
`IncidentDecision` JSON Schema约3,501字符。旧Runtime把系统提示词、JSON Schema和用户消息都放进
argv，按Windows实际转义规则生成的命令行约39,977字符，超过32,767字符上限约7,210字符。因此
根因不是单个文件名或项目绝对路径，而是“把大内容当作进程参数”的传输设计。

### 34.2 方案选择

本次采用内容与控制参数分离：固定选项、工具白名单、短路径和有界JSON Schema保留在argv；完整
系统提示词写入每轮独占的UTF-8临时文件，通过Claude Code原生
`--append-system-prompt-file`读取；用户消息通过`--input-format text`的stdin发送。流式Popen和最终
JSON subprocess.run共用这一传输协议，避免开发与异常流程再次分叉。

没有采用以下看似简单的方案：

1. 截断数据库Schema或系统规则：虽然可以暂时启动，但会静默丢失表字段或安全约束，使诊断结果
   不稳定；
2. 改用PowerShell字符串或`shell=True`：不能消除底层长度限制，还会扩大转义和命令注入风险；
3. 把大内容放入环境变量：仍受进程环境块限制，生命周期和敏感信息暴露范围更难控制；
4. 只提高路径长度策略：Windows长路径开关针对文件系统路径，不会提高CreateProcess命令行上限。

### 34.3 核心实现与安全边界

`ClaudeCodeRuntime._launch_invocation()`负责创建临时目录、以UTF-8写入系统提示词、构造安全命令、
做长度预检并在作用域退出时清理。非流式路径通过`subprocess.run(input=...)`发送用户消息；流式
路径建立stdin pipe并由独立线程写入，stdout/stderr读取和中断语义保持不变。即使模型失败、进程
超时或用户中断，外层临时目录仍会清理。

预检使用`subprocess.list2cmdline()`按Windows参数转义方式计算最终长度，在达到32,767字符前返回
明确的宿主错误。运行日志新增`transport`、`command_chars`、`system_prompt_chars`、
`user_message_chars`和`json_schema_chars`，但不记录正文、临时文件内容、SQL结构或凭据。测试使用
超过100KB的系统提示词验证正文不在argv、文件在子进程启动时可读、结束后已删除，最终命令仍在
限制以内；流式路径另外验证stdin pipe和相同的临时文件生命周期。

### 34.4 更进一步的优化方向

文件传输解决的是操作系统可靠性，不会减少发送给模型的Token。当前异常流程仍一次注入最多1000个
字段元数据，面对更大数据库可能增加上下文成本和相关信息噪声。后续更合理的独立迭代是建立按需
Schema发现协议：先向模型提供有界表目录，允许模型根据页面代码、SQL片段和用户问题请求相关表的
字段，再生成业务数据查询；查不到时可逐级扩大范围。该改造必须保留确定性的只读校验、查询轮次、
行数和60秒超时，且需要评测页面定位与异常诊断召回率，不能仅通过Prompt删减宣称完成。

### 34.5 决策记录

- ADR-099：大系统提示词使用每轮临时文件传递，不再直接进入argv；
- ADR-100：用户消息统一通过stdin传递，流式与非流式Runtime保持同一输入协议；
- ADR-101：Windows启动前按实际转义结果检查32,767字符边界，日志只记录长度而不记录内容；
- ADR-102：本次不截断数据库Schema；按需Schema发现作为需要独立协议和质量评测的后续优化。
