# AutoCodingEngineerCoreNew 桌面 UI 设计规范

## 1. 设计方向

本项目采用“AI Coding Agent 的任务输入结构 + Apple Vision Pro 式浅色玻璃层次 + Fluent 2
桌面交互令牌”。`v0.5.1` 的直接视觉依据是用户提供的三栏桌面工作台参考图；外部来源只用于
补充组件与可访问性交互原则：
参考来源：

- [Vercel Chatbot Template](https://vercel.com/templates/ai/chatbot)：对话阅读区和固定输入区；
- [Vercel Coding Agent Template](https://vercel.com/templates/ai/coding-agent-template)：聚焦任务输入、
  Agent/模型上下文和单一主操作；
- [Fluent 2 Design Tokens](https://fluent2.microsoft.design/design-tokens)：语义颜色、间距、字号和状态令牌；
- [Fluent 2 Button Guidance](https://fluent2.microsoft.design/components/web/react/core/button/usage)：
  一个表面只突出一个主操作，并降低次要动作的强调度。

当前实现是 Windows 原生 Tkinter 客户端，必须保留系统字体、键盘焦点、本地执行、审批和恢复
语义。Tk 8.6 没有浏览器的 `backdrop-filter` 和逐层 RGBA 模糊，所以使用圆角 Canvas、浅色分层、
白色内高光和低对比阴影近似玻璃材质，不宣称实现真实光学模糊。

## 2. 可复用 AI UI 提示词

```text
Design a premium desktop AI engineering workspace with an Apple Vision Pro
inspired glass aesthetic. Use a soft white/silver background, a 280px floating
left task navigation, a central conversation and task-context column, and a
right operational overview backed only by real application state. Use 20-24px
card radii, low-contrast diffuse shadows, white inner highlights, Apple Blue
#2563EB for the current flow and primary action, generous breathing room, and
high-readability Segoe UI Variable / Microsoft YaHei typography. Keep surfaces
calm, professional and enterprise-grade. Avoid neon, gaming style, strong
gradients, decorative clutter and invented metrics. Preserve model autonomy
while making task state, permissions, local logging and recovery explicit.
```

## 3. 核心视觉令牌

| 语义 | 值 | 用途 |
| --- | --- | --- |
| Window | `#EEF3FA` | 白银/轻蓝应用背景 |
| Ambient | `#E7EFFB` | 低强度环境光层 |
| Glass | `#F9FBFE` | 悬浮玻璃卡片近似色 |
| Glass floating | `#F5F8FC` | 指标卡、状态卡 |
| Surface | `#FCFDFE` | 顶栏、输入卡和状态卡 |
| Surface subtle | `#F8FAFC` | 侧栏与轻量控件 |
| Border | `#FFFFFF` | 内高光边界 |
| Border soft | `#DCE5F0` | 输入与未选控件边界 |
| Shadow | `#D9E3F0` | 低对比悬浮阴影 |
| Text | `#111827` | 主文本 |
| Muted | `#475569` | 描述、元信息 |
| Accent | `#2563EB` | 当前流程、主操作、焦点 |
| Success | `#15803D` | 完成 |
| Warning | `#B45309` | 待输入、待审批、恢复提示 |
| Danger | `#DC2626` | 失败和高风险拒绝 |

## 4. 布局与交互规则

1. 左侧导航占约 286 px；品牌、新建任务、历史、配置和日志形成稳定纵向层级。
2. 顶栏横跨工作区，显示流程、标题和文字状态；开发/异常处理使用明确的选中表面。
3. 中栏对话优先保证阅读宽度，任务上下文固定在其下方并保持单一蓝色主操作。
4. 右栏概览展示当前流程真实的今日任务、完成、进行中、完成率、七日趋势与本机服务状态；
   不复制视觉稿中的示例数字。
5. 审批与恢复使用独立警示卡；按钮必须能通过 Tab 聚焦并用 Enter/Space 激活。
6. 窗口宽度低于 1180 px 时隐藏概览区，只保留导航、对话和输入主路径。
7. Hover 只做轻微表面变化，过渡不抢占任务注意力；状态不能只靠颜色表达。

## 5. Tkinter 实现约束

- 主题令牌集中定义，不在业务方法中重复硬编码颜色；
- `GlassPanel` 只负责圆角材质和布局容器，不能复制 Agent 状态或业务统计规则；
- 自定义 Canvas 控件不得覆盖 Tkinter 基类方法或内部属性；
- 状态切换仍由应用内核决定，UI 只读取状态并呈现，不复制流程规则；
- 圆角按钮必须实现 `configure`、`cget`、`invoke` 和禁用态，兼容现有控制逻辑；
- 视觉调整后至少执行桌面 UI 测试、完整非 live 回归、Ruff 和一次真实窗口渲染检查。

## 6. 真实数据口径

- 今日任务：当前流程中 `created_at` 为本地今日的会话数；
- 已完成：当前流程状态为 `completed` 的会话数；
- 进行中：排除 `completed` 和 `failed` 后的会话数；
- 完成率：已完成会话数 / 当前流程全部会话数；
- 七日趋势：按本地日期统计当前流程近七天创建的会话数；
- 运行状态：本地 UI 存活、当前流程项目知识数量、Claude Code 模型就绪状态、SQL Server
  配置状态。模型探测结果在窗口生命周期内缓存，保存新配置时主动刷新。
