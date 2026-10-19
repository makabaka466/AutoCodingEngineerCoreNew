# ACE-DEV-037：合并开发质量规则

- 状态：done，2026-10-19；版本 0.13.1。
- 授权：用户同意在现有开发规则中小幅合并风格、复用、兼容和可读性约束。
- 范围：investigate_code、propose_change、implement_change 三份方法说明及发布文档。
- 约束：保留原阶段加载、工作流和审批；不新增模型轮次；同职责至少两处实际复用才考虑提取，不强制抽象。
- 输入测量：inspect +229 字符（2.18%），implement +562（4.91%），verify +229（2.08%）。
- 验证：当前工作区 323 项非 live 测试通过；Ruff、compileall、diff check 通过。既有阶段加载测试覆盖新增方法正文的装配；未新增逐字镜像规则的测试。
- 保留现场：已有 incident/engine.py 修改与 docs/ERROR_LOG.md 不改、不纳入本次提交。
