# ACE-RUNTIME-020：结构化页面连续性与可复制对话

## 1. 问题

异常调查已验证页面并成功查询业务数据后，模型下一轮 `business_data` 决定漏传重复的 `page`，
旧解析层在 Engine 取得决定前直接失败。桌面对话虽然只读，但缺少清晰、稳定的选择与复制交互。

## 2. 实现

- Session 持久化本 cycle 最近一次已验证 `located_page`；
- Pydantic 负责单轮数据形状，Engine 负责依赖 Session 的页面前置校验；
- 后续 `business_data/completed` 漏页时复用已验证对象并记录 `decision_repaired`；
- 无已验证页面、无源码路径、路径越界和新 cycle 均不允许继承；
- 查询回灌显式提醒模型重复页面上下文；
- transcript 增加文本光标、选区高亮、Ctrl+C/Ctrl+A 和右键复制/全选。

## 3. 验收

- 连续业务查询及完成决定漏页时任务可继续；
- 没有已验证页面时仍被拒绝；
- 页面恢复进入 Event Store，未保存原始业务数据；
- 对话中的用户、Agent、系统和元数据文本均可鼠标选择与复制；
- 完成非 live 测试、Ruff、compileall 和 Git whitespace 检查；
- 发布 v0.7.7 并推送 GitHub。
