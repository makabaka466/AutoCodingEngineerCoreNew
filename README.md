# AutoCodingEngineerCoreNew

一个以 **Agent 开发能力** 为核心、与具体平台解耦的软件开发 Agent。

CLI 和网页只是输入输出媒介；真正的产品是同一套 `AgentApplication`：它把用户任务交给
Claude Code，由模型判断需求是否清楚、应该读哪些文件、如何调查和解决，并在必要时向用户
追问或申请修改/验证权限。

## 核心目标

- 最大限度使用模型和 Claude Code 的理解、检索与开发能力。
- 语义判断由模型完成，不用文件名关键词或固定业务流程替代模型推理。
- 程序只维护必须确定的边界：工作区、工具权限、用户审批、结构化结果、会话和持久化。
- 每个完成任务自动生成一份可复用能力文档，供这个项目后续任务按需参考。
- 保持一个 Agent、一个任务状态机和两个薄入口，不引入无收益的多 Agent 或平台耦合。

## 工作流程

```text
用户任务
  -> 模型判断是否清楚
     -> 不清楚：每轮只问一个最关键问题，并恢复同一 Claude 会话
     -> 清楚：读取目标及必要关联代码
        -> 只读结论：完成
        -> 需要修改：请求 modify 授权
           -> 修改后需要运行命令：请求 verify 授权
              -> 验证并完成
  -> 保存任务会话
  -> 完成时写入能力文档和 CAPABILITIES.md 索引
```

能力记忆默认写入 `~/.autocoding-agent/workspaces/<workspace-id>/`，不会暗中修改目标仓库，
也不会覆盖项目已有的 `CLAUDE.md`。

## 环境要求

- Python 3.12+
- 已安装并可运行的 Claude Code
- 已配置可用的 Anthropic 兼容端点和模型

当前机器可以直接使用：

```powershell
cd D:\learning\project\AutoCodingEngineerCoreNew
D:\python\python.exe -m pip install -e ".[dev,ui]"
Copy-Item .env.example .env
```

`.env` 中至少需要真实 Claude Code 可执行文件：

```dotenv
AUTO_CODING_CLAUDE_COMMAND=D:\claude\node_modules\@anthropic-ai\claude-code\bin\claude.exe
AUTO_CODING_CLAUDE_MODEL=deepseek-v4-pro
AUTO_CODING_CLAUDE_TIMEOUT_SECONDS=600
```

DeepSeek 配置可执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure_deepseek.ps1
```

脚本会交互读取 API Key，并写入当前 Windows 用户环境变量，不会把 Key 保存进项目文件。

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

## 对话 UI

直接双击项目根目录中的 `start.cmd` 即可启动 UI。它会自动调用 PowerShell、绕过脚本执行
策略，并在启动失败时保留窗口显示错误信息。

也可以在终端运行同一个双击脚本：

```powershell
.\start.cmd
```

需要自定义参数时，可以直接调用底层 PowerShell 脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

可选参数：`-Port 8502` 指定端口，`-NoBrowser` 不自动打开浏览器，`-ForceInstall` 强制
重新安装项目和 UI 依赖。

也可以直接使用 Python 启动：

```powershell
D:\python\python.exe -m autocoding_agent.interfaces.streamlit_ui
```

也可以在安装后运行 `autocoding-agent-ui`。页面只调用公共应用接口，不包含另一套分析规则。

## 项目结构

```text
src/autocoding_agent/
├─ application.py          # 所有平台共用的稳定入口
├─ core/                   # 状态、数据契约、权限档位和任务状态机
├─ ports/                  # Runtime / SessionStore 抽象
├─ adapters/               # Claude Code、JSON 会话、能力记忆
├─ skills/                 # 显式注入模型的工作方法
└─ interfaces/             # Typer CLI 与 Streamlit UI
tests/                     # 不消耗模型额度的确定性测试；live 测试单独标记
docs/                      # 架构与接口说明
```

详细内容见 [架构与结构文档](docs/ARCHITECTURE.md) 和
[接口文档](docs/INTERFACES.md)。

版本发布遵循“一次版本、一次上传、一个不可变 tag”，具体步骤见
[版本发布与回退](RELEASING.md)。

## 验证

```powershell
D:\python\python.exe -m ruff check src tests
D:\python\python.exe -m pytest -m "not live"
```

真实模型测试会调用当前配置的 Claude Code，需单独运行：

```powershell
D:\python\python.exe -m pytest -m live
```

第一版有意不实现多 Agent、远程任务平台、自动跨项目记忆、流式 token 展示和任意 Shell
执行。这些都不是当前核心目标；以后可以通过 `AgentApplication` 或 ports 增加，而无需改写内核。
