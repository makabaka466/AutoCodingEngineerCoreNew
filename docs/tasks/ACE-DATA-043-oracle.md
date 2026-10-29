# ACE-DATA-043 Oracle 只读数据源

- 状态：done
- 授权：2026-10-29，用户要求进入第二阶段，并提供 JDBC 19.3.0.0/ojdbc8 驱动截图。
- 目标：Oracle 与 SQL Server 可在同一项目绑定；配置、连接测试、参数查询、来源审计与只读边界一致。
- 范围：python-oracledb 3.4.x、Service Name/SID、Thin/Thick、环境/Owner、只读查询和桌面配置；不包含页面定位重构或数据库写入。
- 已确认：JDBC 版本不等于服务端版本；QTMES2 引用的 Oracle.DataAccess 10.2 也只是客户端线索。
- 决策：默认 Thin；需要旧库或原生客户端时显式 Thick。进程中不静默切换驱动模式；连接测试返回服务端版本。
- 验收：混合源路由、绑定隔离、参数与只读校验、结果截断与脱敏、LOB 有界读取、连接失败不泄露凭据、全量回归。
- 当前：第二阶段代码完成。用户已确认服务端 19.15.0.0.0，默认 Thin；下一步用户在数据库配置中填写并测试真实连接，然后绑定项目。未使用仓库中的凭据。
- 验证与交付：0.17.0；最终 382 passed（123.08s），新增 25 项覆盖 Oracle 适配、混合路由、两套流程、UI、模式、参数与来源隔离；Ruff、compileall、diff --check 通过。唯一警告为上游 StarletteDeprecationWarning。按 RELEASING.md 发布单一可回退提交。
- 官方依据：https://python-oracledb.readthedocs.io/en/v3.4.2/user_guide/installation.html

- 环境：已安装并核验 python-oracledb 3.4.2；启动脚本新增驱动依赖检查。
- 文档：README、ARCHITECTURE、INTERFACES、PROJECT_EXPERIENCE、API 和 ORACLE 配置说明已同步。
- 限制：未连接真实业务库；Thick 需原生客户端且模式属于整个进程；无数据库写入、钱包管理或页面定位变更。
- 用户文件：原 incident/engine.py 的 31 行注释及知识文件保留，不纳入提交。
