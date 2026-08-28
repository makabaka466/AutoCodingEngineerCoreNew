# 任务记录：ACE-HERMES-014 Hermes Skill 只读能力接入

- 任务编号：`ACE-HERMES-014`
- 当前状态：`done`
- 下一步责任方：用户配置 Hermes 模型后执行一次无敏感数据的 live smoke test
- 创建日期：2026-08-28
- 用户授权：完成 ACE 直接使用 Hermes Skill 的第一版；每次迭代提交、标记版本并推送 GitHub。
- 修改前回退点：tag `v0.6.4`，提交 `97997cd599755d53095a3a1ddcbb1c801b65760c`。

## 目标

把 Hermes 作为可选的“工程经验 Skill 提供者”接入开发与异常处理工作流。由 Claude/ACE 判断何时需要某个明确 Skill，ACE 负责白名单校验和审计，Hermes 只返回候选经验，不取得代码修改、数据库查询、状态转换或审批权限。

## 范围与安全边界

- 自动发现 `HERMES_HOME/skills/<category>/<skill>/SKILL.md`，只暴露允许分类中的精简目录；
- 通过 Hermes CLI 显式预加载一个 Skill，跳过自动规则/Memory，以只读 Web toolset 和中立工作目录运行；
- 不向 Hermes 传递 API Key、连接串、完整业务数据、数据库查询结果或未脱敏敏感信息；
- Hermes 输出按“不可信候选证据”回灌 Claude，由 Claude 结合项目代码、数据库证据和既有规则判断；
- Hermes 不得改变 TaskState、执行 SQL、修改代码、批准变更或绕过验证；
- CLI 缺失、模型未配置、超时、返回码异常或输出无效时，记录失败事件并自动回到原 Claude 流程；
- 首版不共享 Hermes Memory、不导入完整 Hermes Runtime、不提供 Skill 写入/同步能力。

## 验收标准

- 存在独立 `HermesSkillService` 端口、请求/结果模型和 CLI 适配器；
- 配置支持自动检测命令与 `HERMES_HOME`，并允许环境变量覆盖、超时及分类白名单；
- 开发和异常处理模型都能结构化请求一个 Skill，宿主限制单轮调用次数；
- 调用全程产生 requested/completed/failed Event，并保存脱敏 Artifact；
- UI 可展示“正在咨询工程经验”，但不会伪装成持久任务状态；
- 无 Hermes 或调用失败时，现有工作流保持可用；
- 通过新增适配器/工作流测试，以及完整非 live 测试、Ruff、compileall、`git diff --check`；
- README、架构、接口和《AutoCodingEngineerCoreNew 项目开发与工程经验》同步更新；
- 单独提交、标记版本并推送 `origin/main`。

## 决策记录

- Hermes 是可替换外部能力提供者，不是 ACE 状态机的上层控制器；
- 模型负责语义判断，宿主只负责权限、预算、路径、超时、脱敏和审计等硬边界；
- 首版每个用户命令最多咨询一次 Hermes，避免模型与外部 Agent 形成无限调用环；
- 仅在 inspect 阶段允许请求 Hermes Skill，implement/verify 阶段拒绝新增请求；
- Skill 名称必须来自动态发现目录并做精确匹配，禁止任意路径和目录穿越；
- 自动化测试使用 Fake Service/Runner，不依赖真实 Hermes 模型配置。

## 当前发现

- Hermes CLI 位于 `D:\learning\tool\hermes-home\bin\hermes.exe`，数据目录为 `D:\learning\tool\hermes-home`；
- 当前 Hermes 模型尚未配置，因此本次只完成可测试的接口与安全降级，真实模型联调作为后续验证；
- Hermes 0.20.6 支持 `chat --query-file - --skills <name> --ignore-rules --toolsets web --quiet --source tool`；`--safe-mode` 会额外忽略用户模型/provider 配置，因此不适合本适配器；
- Skill 当前按分类嵌套存储，首版采用动态发现而非硬编码名称。

## 验证记录

| 日期 | 验证 | 结果 | 结论 |
| --- | --- | --- | --- |
| 2026-08-28 | 修改前远端基线 | `origin/main` 与 `v0.6.4` 均指向 `97997cd` | 具备明确回退点 |
| 2026-08-28 | Hermes CLI 参数与源码核对 | 0.20.6 支持 stdin、Skill、toolset、quiet、ignore-rules、source 和 max-turns；显式 Skill 在 ignore-rules 下独立加载 | 改用 `--ignore-rules`，保留用户模型/provider 配置，不使用会屏蔽 config.yaml 的 safe-mode |
| 2026-08-28 | 本机自动发现 | 识别 `D:\learning\tool\hermes-home`、真实 exe 和 25 个允许分类 Skill | 配置与动态目录发现可用，未调用模型 |
| 2026-08-28 | 自动化测试 | `174 passed` | 适配器、开发、异常、失败降级及既有回归全部通过 |
| 2026-08-28 | 静态与构建检查 | Ruff、`compileall`、`git diff --check` 全部通过 | 发布门禁通过 |
| 2026-08-28 | Hermes live 模型调用 | 未执行：Hermes 模型尚未配置 | 不影响主流程；配置模型后仍需补一次 live smoke test |

## 交付与发布

- Hermes 端口与 CLI 适配器；
- 双流程接入、事件/Artifact、进度映射和降级处理；
- 自动化测试与项目文档；
- 目标分支：`origin/main`；发布版本：`v0.7.0`；
- 回退方式：切换到 `v0.6.4` 或提交 `97997cd599755d53095a3a1ddcbb1c801b65760c`；
- 剩余风险：尚未验证用户未来选择的 Hermes provider/model、网络和真实 Skill 响应质量；外部建议
  继续按 `host_verified=false` 处理，不能直接作为修改或诊断事实。
