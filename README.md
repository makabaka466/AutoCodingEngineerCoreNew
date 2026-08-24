# AutoCodingEngineerCoreNew

默认双击 `start.cmd` 会启动桌面客户端。

一个以 **Agent 专业能力** 为核心、与具体平台解耦的任务内核。目前包含两条彼此独立、
共享 Claude Code Runtime 的流程：软件开发，以及页面与业务数据联合诊断的异常处理。

原生桌面客户端、CLI 和备用网页只是输入输出媒介；真正的产品是应用内核。它们把用户任务
交给 Claude Code，由模型判断需求是否清楚、应该读哪些文件、如何调查和解决；Python 主机
只控制文件修改、命令和数据库访问等硬边界。

## 核心目标

- 最大限度使用模型和 Claude Code 的理解、检索与开发能力。
- 语义判断由模型完成，不用文件名关键词或固定业务流程替代模型推理。
- 程序只维护必须确定的边界：工作区、工具权限、用户审批、结构化结果、会话和持久化。
- 每个完成任务自动生成一份可复用能力文档，供这个项目后续任务按需参考。
- 开发任务和异常工单使用各自清晰的状态机，共享模型运行时，不引入平台耦合。

## 开发工作流程

```text
用户任务
  -> 模型判断是否清楚
     -> 不清楚：每轮只问一个最关键问题，并恢复同一 Claude 会话
     -> 清楚：读取目标及必要关联代码
        -> 需要核对业务数据：模型提出最小只读 SQL 查询，由主机执行后继续同一任务
        -> 只读结论：完成
        -> 需要修改：先展示现状、修改方案、目标效果、影响、验证计划和可用预览
           -> 用户确认方案后：请求并使用 modify 授权实施
              -> 修改后需要运行命令：请求 verify 授权
              -> 验证并完成
  -> 保存任务会话
  -> 完成时写入能力文档和 CAPABILITIES.md 索引
```

能力记忆不会覆盖目标仓库已有的 `CLAUDE.md`。用户维护的基础知识直接保存在本项目的
`knowledge/development/` 和 `knowledge/incident/`，结构为
`<二级路径>/<二级路径名>.md`。系统配置中的“MD 能力配置”可切换两套流程并添加二级路径；
每个二级路径唯一对应一份同名 Markdown，页面显示项目相对路径。对话页的“项目”选择框列出
当前流程的二级路径；新任务只会把所选项目的 MD 同步到
`~/.autocoding-agent/workspaces/<workspace-id>/` 的只读能力视图；任务记录与自动生成的
能力文档仍按目标工作区、按流程分开保存。

## 异常处理流程（首版框架）

```text
问题描述 + 页面线索
  -> 模型判断信息是否足够
     -> 不足：只追问一个最关键问题
     -> 足够：定位页面、请求链路、服务与数据访问代码
        -> 模型提出最小只读 SQL 查询计划
        -> 主机校验只读边界、限制行数并脱敏敏感列
        -> 模型结合代码与查询结果给出诊断、证据、建议和自动化候选判断
```

首版只做诊断，不写数据库、不修改代码、不自动执行修复。模型负责理解页面和选择有价值的
查询；宿主负责 SQL Server 只读连接、SQL 写操作拦截、5 秒查询超时、最多两轮查询、每条
最多 50 行，以及
password/token/secret 等敏感列脱敏。接口已预留 `source` 与 `external_reference`，后续钉钉
机器人可以直接创建和继续同一异常会话。

## 环境要求

- Python 3.12+
- Claude Code（客户端启动时自动检测；未检测到时会在配置页提示安装或选择 `claude.exe`）
- 一个可用的 Anthropic 兼容端点、模型名和 API Key
- 两套流程需要查询业务数据时，需安装 Microsoft ODBC Driver 17 或 18 for SQL Server

安装项目依赖：

```powershell
cd D:\learning\project\AutoCodingEngineerCoreNew
D:\python\python.exe -m pip install -e ".[dev,ui]"
```

随后直接双击 `start.cmd`。客户端会先搜索并运行真实 `claude.exe --version`：如果 Claude Code、
API 地址、模型名或 API Key 任一项未就绪，会先显示“系统配置”。同一个窗口包含“模型与
Claude Code”“SQL Server”“MD 能力配置”三个页签：模型页支持自动检测和手动选择
`claude.exe`；数据库页可以测试、保存和随时更换两套流程共用的只读连接；MD 页按流程和
二级路径管理可编辑知识，点击添加会创建路径及其同名 Markdown。API Key 与数据库密码都不会回填显示。

当前默认值适用于 DeepSeek Anthropic 兼容接口：

```dotenv
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
AUTO_CODING_CLAUDE_MODEL=deepseek-v4-pro
```

主界面左侧的“系统配置”可随时更换端点、模型、密钥、SQL Server 连接或 MD 能力配置。`.env.example` 和旧的 PowerShell
配置脚本仍保留给自动化或高级部署使用，不再是桌面客户端的必需步骤：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure_deepseek.ps1
```

脚本同样只把 API Key 写入当前 Windows 用户环境变量。

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

客户端使用白色浅色主题，顶部胶囊按钮可明确选择“开发”或“异常处理”，蓝色按钮表示当前
流程；两套流程分别加载自己的历史会话。对话输入区的“项目”选择框决定本任务使用哪个二级
路径的 MD，所选项目会随会话保存且任务开始后不可切换。代码项目路径位于消息输入框上方；选择异常处理时会额外
显示异常页面线索，不再重复显示数据库配置框；两套流程的 SQL Server 统一从左侧“系统配置”
管理。非密钥配置保存在本机用户数据目录，密码保存到 Windows 凭据管理器；已有
密码不会回填。开发和异常处理都能让模型按需提出只读查询计划；当前会话保持启动时的连接，
更换配置从该流程的下一项任务开始生效。左侧保留新建任务、最近任务、系统配置和本地日志入口。开发
流程提供多轮对话、澄清、方案预览、修改/验证授权、任务结果和能力文档提示。修改
方案会明确展示每项内容“现在是什么、要改成什么”，以及目标效果、影响、验证方式和适合
当前任务的界面线框、接口示例、伪代码或行为前后对比。模型执行在后台线程中进行，等待
Claude Code 时窗口仍可正常刷新，但为保护同一会话，本轮结束前会禁用重复提交。桌面端采用
单实例运行，再次双击会提示切换到已有窗口。

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
├─ core/                   # 状态、数据契约、权限档位和任务状态机
├─ incident/               # 异常工单契约、页面定位/数据诊断状态机与应用门面
├─ ports/                  # Runtime / SessionStore / 通用结构化 Runtime 抽象
├─ adapters/               # Claude Code、JSON 会话、能力记忆、SQL Server/SQLite 只读数据源
├─ skills/                 # 显式注入模型的工作方法
└─ interfaces/             # 原生桌面客户端、Typer CLI 与备用 Streamlit UI
tests/                     # 不消耗模型额度的确定性测试；live 测试单独标记
docs/                      # 架构与接口说明
```

详细内容见 [架构与结构文档](docs/ARCHITECTURE.md) 和
[接口文档](docs/INTERFACES.md)。

`v0.2.x` 等补丁版本只在本地迭代；只有中间版本位递增，例如升级到 `v0.3.0` 时，才向
GitHub 发布一次并创建不可变 tag。具体步骤见[版本发布与回退](RELEASING.md)。

## 验证

```powershell
D:\python\python.exe -m ruff check src tests
D:\python\python.exe -m pytest -m "not live"
```

真实模型测试会调用当前配置的 Claude Code，需单独运行：

```powershell
D:\python\python.exe -m pytest -m live
```

当前有意不实现多 Agent、流式 token、数据库写入、异常自动修复和钉钉机器人。桌面客户端
已经可以手动切换并使用两套流程；后续钉钉只需
调用 `IncidentApplication`，数据库类型则通过 `DatabaseReader` 端口扩展，无需改写诊断内核。
