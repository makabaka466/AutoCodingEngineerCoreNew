# 任务记录：ACE-RAG-010 Voyage Embedding 配置与正式适配器

- 任务编号：`ACE-RAG-010`
- 当前状态：`done`
- 下一步责任方：用户在系统配置中填写 Voyage API Key 并执行一次真实“测试连接”
- 创建日期：2026-08-26
- 用户授权：Embedding 改用 Voyage，并像现有 DeepSeek 模型一样提供配置页面；完成后按既有约定发布一次。
- 目标版本：`v0.6.1`
- 基线与回退点：`v0.6.0` / commit `9e76bd6`

## 目标

把当前只能明确标识为模拟的 Embedding 接口升级为可配置的 Voyage REST Adapter，同时保留无配置
环境下的 Fake 降级路径。系统配置页允许编辑、测试和保存 API 地址、模型、输出维度与 API Key；
RAG 管理页和新任务使用保存后的正式配置。

## 验收标准

- 新增 secret-free Embedding 配置模型、原子 JSON 配置存储和 OS 凭据存储；
- API Key 不回填、不写项目、任务卡、日志或异常正文；空 Key 保存时保留已有凭据；
- 默认端点为 `https://api.voyageai.com/v1/embeddings`，默认模型为 `voyage-code-4`；
- Voyage Adapter 使用 Bearer 认证，文档/查询分别发送 `input_type=document/query`；
- 配置页面支持连接测试、保存和状态展示，测试在后台线程执行；
- 未配置时继续使用 `fake-hash-embedding-v1`，配置完成后新建 RAG 服务使用 Voyage；
- 端点、模型或维度变化生成独立索引身份，旧模拟/旧模型索引不得冒充或自动迁移；
- 新索引仍由用户在知识库管理页手动建立，切换配置后源文档显示待加入；
- 开发与异常流程的新任务共享当前 Embedding 配置，现有活动任务不被静默切换；
- 完整非 live 测试、Ruff、编译和 diff 检查通过；同步 README、架构、接口和项目经验；
- 单一 commit/tag 推送到 `origin/main`，失败时停止在 `v0.6.0` 回退点。

## 已确认设计

- 使用 Voyage 官方 REST 契约，不增加 Voyage SDK 依赖；
- 使用 `input_type` 区分 document/query，保留当前 Markdown Chunk 和 RRF；
- API Key 使用 Windows Credential Manager（keyring），非密钥配置保存到用户数据目录；
- 当前正式向量仍存本地 SQLite 可重建向量表；外部 Vector DB 作为独立后续迭代；
- 索引数据库名由 provider/endpoint/model/dimension 指纹隔离；源 Markdown 不移动、不删除；
- 配置保存不等于索引上传，必须由用户在知识库管理页再次确认。

## 影响与未授权范围

- 影响：Embedding 配置/凭据、Voyage HTTP Adapter、RAG 组合根、系统设置 UI、桌面新任务、测试和文档；
- 外部调用：仅用户点击“测试连接”或手动建立/查询正式索引时调用所配置的 Embedding API；
- 外部发布：完成验证后按授权推送一个版本；
- 未授权：不自动上传现有 Markdown、不删除旧索引、不接入外部向量数据库、不记录 API Key；
- 失败停止条件：密钥泄露、索引身份混用、现有任务静默切换、回归或持久化边界失败时不发布。

## 验证记录

| 日期 | 验证 | 结果 | 结论 |
| --- | --- | --- | --- |
| 2026-08-26 | 官方 Voyage Embeddings REST、模型和 input_type 契约核对 | 已完成 | 可按 Bearer + POST /v1/embeddings 实现无 SDK Adapter |
| 2026-08-26 | 配置、Voyage REST、RAG 和桌面聚焦测试 | 39 项通过 | 密钥、请求角色、维度、索引隔离和 UI 契约通过 |
| 2026-08-26 | 完整非 live 回归 | 160 passed | 无既有功能回归 |
| 2026-08-26 | Ruff / compileall / git diff --check | 通过 | 代码、导入、语法和补丁格式符合发布门禁 |
| 2026-08-26 | 真实 Voyage 账号连接 | 待用户配置后执行 | 未持有用户 API Key，本迭代不伪造 live 成功 |

## 交付物

- Embedding 配置/凭据与 Voyage Adapter
- 通用 SQLite VectorStore 和配置化 RAG 组合根
- 系统配置 Embedding 页签及桌面接入
- 自动化测试与完整项目文档

## 发布记录

- 提交：`feat: add configurable Voyage embedding provider`
- 标签：`v0.6.1`
- 推送目标：`origin/main` 与 `v0.6.1`（本任务发布步骤执行）
