# AutoCodingEngineerCoreNew

默认双击 `start.cmd` 会启动桌面客户端。

一个以 **Agent 专业能力** 为核心、与具体平台解耦的任务内核。目前包含两条彼此独立、
共享 Claude Code Runtime 的流程：软件开发，以及页面与业务数据联合诊断的异常处理。两条流程
还可以按需咨询本机 Hermes Skill，把通用工程经验作为候选证据交回主模型核验。

原生桌面客户端、CLI 和备用网页只是输入输出媒介；真正的产品是应用内核。它们把用户任务
交给 Claude Code，由模型判断需求是否清楚、应该读哪些文件、如何调查和解决；Python 主机
只控制文件修改、命令和数据库访问等硬边界。

## 核心目标

- 最大限度使用模型和 Claude Code 的理解、检索与开发能力。
- 语义判断由模型完成，不用文件名关键词或固定业务流程替代模型推理。
- 程序只维护必须确定的边界：工作区、工具权限、用户审批、结构化结果、会话和持久化。
- 每个完成会话自动生成一份可复用能力文档；同一会话续聊后的完成内容追加到原文档。
- 开发任务和异常工单共享 TaskState、Event、Runtime Run 与恢复扫描内核，同时保留各自的业务决定。
- 两套流程的 Task/Event/Run 由同一个 runtime SQLite 数据库原子保存，可回放、可审计并支持命令幂等。
- Hermes 是可选的只读工程经验提供者，不控制状态机，也不能修改代码或查询业务数据库。
- 写阶段中断不自动重试；系统生成恢复证据，由用户选择只读检查、重新规划或取消。

## 开发工作流程

```text
用户任务
  -> 模型判断是否清楚
     -> 不清楚：每轮只问一个最关键问题，并恢复同一 Claude 会话
     -> 清楚：读取目标及必要关联代码
        -> 需要通用工程方法：可选择一个已安装 Hermes Skill，只读咨询后由 Claude 核验
        -> 需要核对业务数据：模型提出最小只读 SQL 查询，由主机执行后继续同一任务
        -> 只读结论：完成
        -> 需要修改：先展示现状、修改方案、目标效果、影响、验证计划和可用预览
           -> 用户确认方案后：请求并使用 modify 授权实施
              -> 修改后需要运行命令：请求 verify 授权
              -> 验证并完成
  -> 保存任务会话
  -> 首次完成时写入会话能力文档和 CAPABILITIES.md 索引
  -> 用户继续追问：同一会话开启下一工作轮次，重新分析并在完成时追加原 MD
```

能力记忆不会覆盖目标仓库已有的 `CLAUDE.md`。用户维护的基础知识直接保存在本项目的
`knowledge/development/` 和 `knowledge/incident/`，结构为
`<二级路径>/<二级路径名>.md`。系统配置中的“MD 能力配置”可切换两套流程并添加二级路径；
每个二级路径唯一对应一份同名 Markdown，页面显示项目相对路径。对话页的“项目”选择框列出
当前流程的二级路径；新任务只会把所选项目的 MD 同步到
`~/.autocoding-agent/workspaces/<workspace-id>/` 的只读能力视图；任务记录与自动生成的
能力文档仍按目标工作区和流程分开保存；每个 Session 对应一份自动能力 MD，后续完成内容以
明确的工作轮次章节追加。新建任务会创建新 Session，因此会形成新的 MD。

### 手动 RAG 知识库与 Voyage Embedding

左侧“知识库管理”会发现 Project Knowledge、两套流程生成的 Capability，以及本项目的工程
经验文档，但不会自动上传或索引。用户可以预览 Markdown 分块，手动选择“加入 / 重建索引”、
移除索引或测试检索；移除只删除可重建索引，不删除源 Markdown。文档修改后会显示“内容已更新”，
由用户决定何时重建。

系统配置的“Embedding”页可保存 Voyage API 地址、模型名、输出维度和 API Key，并在后台测试
连接。默认使用 `https://api.voyageai.com/v1/embeddings`、`voyage-code-4` 和 1024 维；文档与查询
分别使用 `input_type=document/query`。API Key 保存到 Windows 凭据管理器，不回填到页面。

未配置 Voyage 时，系统继续使用明确标识的 `fake-hash-embedding-v1`，只验证工作流；配置完成后，
知识库管理页显示正式 Voyage 模型身份。不同 API 地址、模型或维度使用独立的本地 SQLite 向量/
FTS5 数据库，切换配置不会复用旧模拟向量，也不会自动上传 Markdown；用户需要重新选择文档并
手动建立索引。查询同时取得 Dense Top 20 与 BM25 Top 20，再通过 RRF 融合，默认向 Agent 返回
最多 6 个 Chunk、每篇文档最多 2 个。检索失败会留下事件并降级为无 RAG 继续执行。

### Hermes 工程经验 Skill（第一版）

ACE 会从 `HERMES_HOME/skills/<category>/<skill>/SKILL.md` 动态发现允许分类的 Skill，并只把名称、
分类和简短描述放入 Claude 提示词。Claude 只有在 inspect 阶段认为通用工程方法能显著帮助当前
任务时，才会结构化请求一个精确 Skill；宿主随后让 Hermes 跳过自动规则/Memory，仅开放只读 Web toolset、
中立工作目录和隐藏控制台执行咨询。整段用户对话、源码、数据库结果和工作区不会被自动转发。

Hermes 输出会脱敏、截断并标记为“不可信候选工程经验”，再交回当前 Claude 会话结合代码和数据
证据核验。每个命令默认最多咨询一次；Hermes 未安装、ACE 模型配置缺失、超时或返回错误时会留下事件与
Artifact，并自动继续原有 Claude 流程。首版不共享 Hermes Memory，不让 Hermes 修改项目、执行
SQL、改变 TaskState 或绕过审批。

## 异常处理流程

```text
问题描述 + 可选页面标题/路径 + 可选异常截图
  -> 模型先理解对话中的标题、路径、菜单入口和异常上下文
  -> 有截图时再分析图片中的页面标题、菜单和异常区域
     -> 对话/图片任一方有可信页面身份：进入定位
     -> 图片无标题但对话有标题/路径：按对话线索定位后与图片比对
     -> 对话、图片和代码候选冲突且证据不足：请用户确认异常页面
     -> 对话和图片都无可信标题/路径：只追问一个页面线索，不扫描全部页面
  -> 路径可信：直接读取代码验证；标题需映射：先有界精确/前缀，无结果再有界模糊查询
  -> 模型结合对话、映射 URL、当前代码以及可用截图验证页面
     -> 需要通用诊断方法时：可只读咨询一个 Hermes Skill，再核对当前页面、代码和数据证据
     -> 定位页面、请求链路、服务与数据访问代码
        -> 页面映射或业务数据在数据库：模型从代码/schema 提取最小参数化只读 SQL
        -> 主机自动校验并执行，不要求用户手工查询
        -> SQL 失败：脱敏错误自动返回模型，在轮次上限内自行修正
        -> 模型结合代码与查询结果给出诊断、证据、建议和自动化候选判断
  -> Runtime 中断：启动扫描后暂停，由用户选择继续只读调查、重新调查或取消
```

当前只做诊断，不写数据库、不修改代码、不自动执行修复。模型负责理解页面、阅读相关代码并
形成结构化查询；宿主直接执行查询并负责 SQL Server 只读连接、SQL 写操作拦截、60 秒查询超时、
最多两轮查询、每条默认最多 100 行，以及
password/token/secret 等敏感列脱敏。接口已预留 `source` 与 `external_reference`，后续钉钉
机器人可以直接创建和继续同一异常会话。审计只保存 SQL 指纹、参数名、用途、行数和脱敏信息，
不保存参数值与原始业务行。结果数量不明确时，模型会先使用 100 条有界采样；能用更少数据
完成判断时仍优先采用更小限制。

通用异常调查规则作为应用内置的异常专用 Markdown 每轮强制加载；规则描述证据顺序和澄清边界，
页面标题/路径可信度、截图标题、异常区域和候选匹配仍由模型判断。项目表结构、页面映射方式和
代码架构保存在用户选择的 `knowledge/incident/<项目>/<项目>.md`。现有“生物”项目继续使用
`knowledge/incident/生物/生物.md`，其中 `Menu.NAME -> Menu.URL` 查询只适用于该项目。每次任务
的具体页面、数据摘要和诊断结论进入独立异常 Capability，人工确认后才可加入 RAG。

## 环境要求

- Python 3.12+
- Claude Code（客户端启动时自动检测；未检测到时会在配置页提示安装或选择 `claude.exe`）
- 一个可用的 Anthropic 兼容端点、模型名和 API Key
- 需要真实语义 RAG 时使用 Voyage Embedding 端点和 API Key；未配置时继续使用模拟检索器
- 可选 Hermes Agent；安装后设置 `HERMES_HOME` 并确保 `hermes.exe` 可执行即可自动发现 Skill
- 两套流程需要查询业务数据时，需安装 Microsoft ODBC Driver 17 或 18 for SQL Server

安装项目依赖：

```powershell
cd D:\learning\project\AutoCodingEngineerCoreNew
D:\python\python.exe -m pip install -e ".[dev,ui]"
```

随后直接双击 `start.cmd`。客户端会先搜索并运行真实 `claude.exe --version`：如果 Claude Code、
API 地址、模型名或 API Key 任一项未就绪，会先显示“系统配置”。同一个窗口包含“模型与
Claude Code”“Embedding”“SQL Server”“项目路径”“MD 能力配置”五个页签：模型页支持自动检测
和手动选择 `claude.exe`；Embedding 页配置、测试并保存 Voyage；数据库页可以测试、保存和随时
更换两套流程共用的只读连接；项目路径页保存两套
流程创建新任务时使用的代码根目录；MD 页按流程和二级路径管理可编辑知识，点击添加会创建路径
及其同名 Markdown。生成模型 Key、Embedding Key 与数据库密码都不会回填显示。

当前默认值适用于 DeepSeek Anthropic 兼容接口：

```dotenv
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
AUTO_CODING_CLAUDE_MODEL=deepseek-v4-pro
```

主界面左侧的“系统配置”可随时更换生成模型、Embedding、密钥、项目路径、SQL Server 连接或 MD 能力配置。`.env.example` 和旧的 PowerShell
配置脚本仍保留给自动化或高级部署使用，不再是桌面客户端的必需步骤：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure_deepseek.ps1
```

脚本同样只把 API Key 写入当前 Windows 用户环境变量。

Hermes 通常无需再次配置模型：系统依次读取 `AUTO_CODING_HERMES_COMMAND`、PATH 和
`HERMES_HOME/bin/hermes.exe`，Windows 下也会读取刚保存但尚未进入当前进程的用户级
`HERMES_HOME`。默认由 ACE 提供已保存的 DeepSeek `/anthropic` 地址和 API Key，Hermes 子进程
固定使用 `deepseek-v4-flash`；密钥只进入该子进程环境，不写入 Hermes `config.yaml`、命令参数、
Prompt、日志或 Artifact。高级部署可用 `AUTO_CODING_HERMES_USE_ACE_PROVIDER=false` 恢复 Hermes
自有 provider 配置，或用 `AUTO_CODING_HERMES_MODEL` 更换其独立模型；
`AUTO_CODING_HERMES_SKILLS_ENABLED=false` 可完全关闭。

## CLI

开始任务：

```powershell
D:\python\python.exe -m autocoding_agent.interfaces.cli start `
  --workspace D:\your-project `
  "修复 src/order.py 中重复扣库存的问题"
```

根据返回的 `session` 继续澄清或处理审批：

```powershell
D:\python\python.exe -m autocoding_agent.interfaces.cli send `
  --session-id <session-id> "错误发生在 cancel_order()"

D:\python\python.exe -m autocoding_agent.interfaces.cli approve --session-id <session-id>
D:\python\python.exe -m autocoding_agent.interfaces.cli reject --session-id <session-id> `
  --reason "先只给出方案"
```

查看结果与最近任务：

```powershell
D:\python\python.exe -m autocoding_agent.interfaces.cli show --session-id <session-id>
D:\python\python.exe -m autocoding_agent.interfaces.cli sessions
```

### 异常处理 CLI

先检查数据库只读连接及可见表结构：

```powershell
D:\python\python.exe -m autocoding_agent.interfaces.incident_cli check-db `
  --database D:\data\application.db
```

输入问题和页面线索并启动诊断：

```powershell
D:\python\python.exe -m autocoding_agent.interfaces.incident_cli start `
  --workspace D:\your-project `
  --page "/orders/42" `
  --database D:\data\application.db `
  "订单 42 一直停留在处理中"
```

如果模型需要补充信息，使用返回的 `session_id` 继续：

```powershell
D:\python\python.exe -m autocoding_agent.interfaces.incident_cli send `
  --session-id <session-id> `
  --database D:\data\application.db `
  "用户点击提交后页面没有报错，订单号是 42"
```

预期结果包含定位页面、相关代码路径、已执行查询的审计摘要、诊断、置信度、建议动作，以及
该问题是否适合后续接入钉钉后自动处理。应用自己的 JSON 会话只保存查询名称、用途、行数和
脱敏列，不保存原始业务行。

## 桌面客户端

直接双击项目根目录中的 `start.cmd`，默认启动原生桌面客户端。客户端不会打开浏览器，
启动完成后命令窗口会自动关闭；初始化失败时会保留窗口或弹出错误提示。

客户端采用面向长任务的浅色玻璃 AI 工程工作台：左侧悬浮任务导航、中部低干扰对话和任务
上下文、右侧真实任务概览，以及只使用一个高强调主操作的按钮层级。概览显示当前流程持久化
会话计算出的今日任务、完成、进行中、完成率和近七日趋势，同时展示项目知识、模型和 SQL Server
的本机配置状态；不使用演示假数据。窗口低于 1180 px 时会自动隐藏概览，优先保留任务主路径。
顶部胶囊按钮可明确选择
“开发”或“异常处理”，蓝色按钮表示当前流程，状态同时使用文字、颜色和边框表达；两套流程
分别加载自己的历史会话。对话输入区的“项目”选择框决定本任务使用哪个二级
路径的 MD，所选项目会随会话保存且任务开始后不可切换。代码项目路径在系统配置中统一保存，
主对话区不再重复显示；已有 Session 始终使用创建时保存的路径，新配置从下一项任务生效。
异常处理不再设置单独的“异常页面”栏，页面名称、路由和现象直接写进消息；也可以在异常输入框
按 `Ctrl+V` 粘贴截图，发送前可查看数量或清除，纯图片同样可以发起诊断。图片会转为 PNG 保存到
本机隔离附件目录，由 Runtime 只读访问，并被明确视为不可信视觉证据。两套流程的 SQL Server
统一从左侧“系统配置”管理。非密钥配置保存在本机用户数据目录，密码保存到 Windows 凭据管理器；已有
密码不会回填。开发和异常处理都能让模型按需提出只读查询计划；当前会话保持启动时的连接，
更换配置从该流程的下一项任务开始生效。左侧保留新建任务、最近任务、系统配置和本地日志入口。开发
流程提供多轮对话、澄清、方案预览、修改/验证授权、任务结果和能力文档提示。修改
方案会明确展示每项内容“现在是什么、要改成什么”，以及目标效果、影响、验证方式和适合
当前任务的界面线框、接口示例、伪代码或行为前后对比。模型执行在后台线程中进行，等待
Claude Code 时窗口仍可正常刷新，但为保护同一会话，本轮结束前会禁用重复提交。桌面端采用
单实例运行，再次双击会提示切换到已有窗口。

`completed` 表示当前工作轮次已经结束，不再永久锁定会话。桌面和备用 Web 页面会保留输入框；
用户继续发送后，状态从 `completed` 转回 `inspecting`，保留原对话、Runtime Session、事件、
决策和产物，同时重置本轮查询/重规划预算。再次完成会把本轮总结追加到当前 Session 原有的
能力文档；开发与异常处理始终使用不同目录和索引，不会混写。

开发与异常处理在对话区顶部共用一个实时任务进度条。主机根据真实执行阶段和脱敏后的
Runtime 工具生命周期展示“准备上下文、检索知识、分析截图、定位页面、阅读代码、查询数据、
修改、验证、沉淀能力”等状态；这些提示不展示模型思维链、完整 SQL、命令参数或密钥。阶段
切换使用轻量淡出/淡入和呼吸指示，短阶段保证最小可见时间；进度回调失败不会影响 Agent
任务，持久任务状态仍由 State Machine 与 Event Store 独立管理。

也可以在终端运行同一个双击脚本：

```powershell
.\start.cmd
```

需要在终端中等待客户端退出以便调试时：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Wait
```

`-ForceInstall` 可强制重新安装项目依赖。也可以直接使用 Python 启动：

```powershell
D:\python\python.exe -m autocoding_agent.interfaces.desktop_ui
```

安装项目后也可运行 `autocoding-agent-client`。

### 本地运行日志

应用日志默认写入：

```text
~/.autocoding-agent/logs/autocoding-agent.log
```

桌面客户端左下角的“打开本地日志”可直接打开目录。日志按 2 MB 轮转并保留 5 份历史文件，
记录会话 ID、模式、模型、工作区、开始/完成、耗时、Token 数量、超时和脱敏后的 Runtime
错误。日志不会记录完整用户问题、系统提示词、API Key 或数据库查询结果。Windows 调用
Claude Code 时使用隐藏窗口参数，每次问答不会再弹出控制台。

## 备用 Web UI

原 Streamlit 页面继续作为备用入口，不再默认启动：

```powershell
.\start.cmd -Web
```

Web 参数仍然兼容：`-Port 8502` 指定端口，`-NoBrowser` 不自动打开浏览器，
`-ForceInstall` 强制安装 Web 依赖。也可以运行
`D:\python\python.exe -m autocoding_agent.interfaces.streamlit_ui` 或安装后的
`autocoding-agent-ui`。桌面和 Web 页面都只调用公共应用接口，不包含另一套分析规则。

## 项目结构

```text
src/autocoding_agent/
├─ application.py          # 所有平台共用的稳定入口
├─ database_models.py      # 两套流程共用的只读查询与审计契约
├─ core/                   # 状态机、Handler、审计、Artifact、恢复和权限边界
├─ incident/               # 异常工单契约、页面定位/数据诊断状态机与应用门面
├─ knowledge_rag/          # 文档发现、Markdown 分块、混合检索协议与当前伪适配器
├─ ports/                  # Runtime / Event / Decision / Artifact / Session 抽象
├─ adapters/               # Claude Code、SQLite 任务事件、Artifact、Git 观察、能力和只读数据源
├─ skills/                 # 显式注入模型的工作方法
└─ interfaces/             # 原生桌面客户端、知识库管理、Typer CLI 与备用 Streamlit UI
tests/                     # 不消耗模型额度的确定性测试；live 测试单独标记
docs/                      # 架构与接口说明
```

详细内容见 [架构与结构文档](docs/ARCHITECTURE.md)、[接口文档](docs/INTERFACES.md) 和
[桌面 UI 设计规范](docs/UI_DESIGN_GUIDE.md)。项目从背景、业务理解、设计决策到真实踩坑与后续知识体系路线的
完整沉淀见 [项目开发与工程经验](docs/PROJECT_EXPERIENCE.md)。

从 `v0.4.0` 开始，每次完成迭代都会同步版本与文档、运行完整检查、形成一个边界清晰的提交，
并向 GitHub 推送一次。里程碑 tag 保持不可变。具体步骤见[版本发布与回退](RELEASING.md)。

## 验证

```powershell
D:\python\python.exe -m ruff check src tests
D:\python\python.exe -m pytest -m "not live"
```

真实模型测试会调用当前配置的 Claude Code，需单独运行：

```powershell
D:\python\python.exe -m pytest -m live
```

当前有意不实现多 Agent、流式 token 展示、数据库写入、异常自动修复和钉钉机器人。桌面客户端
已经可以手动切换并使用两套流程；后续钉钉只需
调用 `IncidentApplication`，数据库类型则通过 `DatabaseReader` 端口扩展，无需改写诊断内核。
