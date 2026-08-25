# AutoCodingEngineerCoreNew 桌面 UI 设计规范

## 1. 设计方向

本项目采用“AI Coding Agent 的任务输入结构 + 对话产品的阅读留白 + Fluent 2 桌面设计令牌”。
参考来源：

- [Vercel Chatbot Template](https://vercel.com/templates/ai/chatbot)：对话阅读区和固定输入区；
- [Vercel Coding Agent Template](https://vercel.com/templates/ai/coding-agent-template)：聚焦任务输入、
  Agent/模型上下文和单一主操作；
- [Fluent 2 Design Tokens](https://fluent2.microsoft.design/design-tokens)：语义颜色、间距、字号和状态令牌；
- [Fluent 2 Button Guidance](https://fluent2.microsoft.design/components/web/react/core/button/usage)：
  一个表面只突出一个主操作，并降低次要动作的强调度。

参考用于提炼信息层级，不复制网页视觉。当前实现是 Windows 原生 Tkinter 客户端，必须保留
系统字体、键盘焦点、本地执行、审批和恢复语义。

## 2. 可复用 AI UI 提示词

```text
Design a light desktop AI engineering workspace for long-running coding and
incident-diagnosis tasks. Use a 260px left navigation for recent tasks; a quiet,
centered conversation canvas; and a fixed bottom composer that groups project
knowledge, workspace, page context, and prompt. Keep one primary action per
surface. Use semantic status chips with text plus color, and separate warning
cards for approval and recovery. Use neutral layered surfaces, 12px corner
radius, 1px borders, compact Segoe UI / Microsoft YaHei typography, and
accessible contrast. Avoid decorative gradients, excessive icons, and visual
effects without operational meaning. Preserve model autonomy while making the
current flow, task state, permissions, local logging, and recovery boundaries
visible and auditable.
```

## 3. 核心视觉令牌

| 语义 | 值 | 用途 |
| --- | --- | --- |
| Window | `#F5F7FB` | 应用背景 |
| Surface | `#FFFFFF` | 顶栏、输入卡和状态卡 |
| Surface subtle | `#F8FAFC` | 侧栏与轻量控件 |
| Border | `#E2E8F0` | 普通分隔与卡片边界 |
| Text | `#0F172A` | 主文本 |
| Muted | `#64748B` | 描述、元信息 |
| Accent | `#2563EB` | 当前流程、主操作、焦点 |
| Success | `#15803D` | 完成 |
| Warning | `#B45309` | 待输入、待审批、恢复提示 |
| Danger | `#DC2626` | 失败和高风险拒绝 |

## 4. 布局与交互规则

1. 左侧导航固定约 268 px；任务历史可滚动，配置与日志入口保持稳定位置。
2. 顶栏显示当前流程、任务标题和文字状态；不让颜色成为唯一状态信号。
3. 对话区优先保证阅读宽度和段落留白，用户、Agent、系统消息使用不同的轻量表面。
4. 输入区先展示任务上下文，再展示问题；“发送任务”是默认唯一高强调操作。
5. 审批与恢复使用独立警示卡；按钮必须能通过 Tab 聚焦并用 Enter/Space 激活。
6. 工作区变窄时优先保留业务字段和主操作，不依赖仅悬停时才可见的功能。

## 5. Tkinter 实现约束

- 主题令牌集中定义，不在业务方法中重复硬编码颜色；
- 自定义 Canvas 控件不得覆盖 Tkinter 基类方法或内部属性；
- 状态切换仍由应用内核决定，UI 只读取状态并呈现，不复制流程规则；
- 圆角按钮必须实现 `configure`、`cget`、`invoke` 和禁用态，兼容现有控制逻辑；
- 视觉调整后至少执行桌面 UI 测试、完整非 live 回归、Ruff 和一次真实窗口渲染检查。
