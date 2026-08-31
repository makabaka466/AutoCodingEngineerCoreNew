# ACE-RUNTIME-021：搜索策略自动纠正与选区层级

## 1. 问题

模型在已定位页面后用明确符号执行目录 Grep，但漏传 `glob/type`；宿主正确阻断调用，却错误地把
一次可纠正参数遗漏升级为整个任务失败。桌面对话正文的卡片背景还会遮住 selection 颜色。

## 2. 实现

- Runtime 将搜索阻断转换为结构化 `RuntimePolicyBlockedError`；
- 可缩窄的 Glob、缺少 Grep 过滤器/上限标为可纠正，路径越界和预算超限不可纠正；
- 开发与异常 inspect 自动返回脱敏原因并允许一次同 Session 修正；
- 原调用结果不采纳，失败 Run、`policy_blocked`、`policy_repair_requested` 保持审计；
- 第二次违规立即失败，implement/verify 不自动重试；
- 将 transcript 的 `sel` 标签提升到所有消息正文背景之上。

## 3. 验收

- 首次缺少 `glob/type` 不再终止整个只读调查；
- 修正 Prompt 明确要求补过滤器、复用现有证据且不得扩大范围；
- 第二次违规、路径越界和预算超限不能利用重试绕过边界；
- “Agent · 回应”标题和正文都显示相同选区高亮并可复制；
- 非 live 测试、Ruff、compileall 与 Git whitespace 检查通过；
- 发布 v0.7.8 并推送 GitHub。
