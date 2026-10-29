# Oracle 数据源配置

ACE 0.17.0 使用 python-oracledb 3.4.x，不加载 JDBC JAR。用户已提供实际数据库版本
19.15.0.0.0；截图里的 ojdbc8 19.3.0.0 是另一个工具的客户端驱动版本，两者不是同一概念。

## 配置步骤

1. 重启 ACE，系统配置 → 数据库 → 新增 Oracle 数据源。
2. 填写名称、实际环境、Owner/Schema、所属子项目和用途。
3. 填写监听地址、端口（默认 1521）、连接类型（service_name 或 sid）和对应值。
   不要粘贴 JDBC URL；例如 JDBC URL 中 `@//host:1521/service` 的 service 对应 Service Name。
   不根据显示名称猜测 SID/Service Name，应使用已有连接的实际值。
4. 填写只读账号和密码。Oracle 19c 默认选择 thin，客户端目录留空。
5. 点击测试连接，只读查询连接身份并返回数据库版本、服务名和当前 Schema，不读取业务表。
6. 保存后到项目维护 → 绑定数据源，按需要设置默认菜单库/业务库。新建任务生效。
   旧任务不会悄悄替换原绑定，连接或环境变化后需核对并新建任务。

密码保存到操作系统凭据管理器，不写入项目 MD、JSON 连接文件或模型上下文；编辑时不回显。
服务部署账号必须拥有自己的对应凭据。数据库 banner 中的 “Production” 是版本字符串的一部分，
不能据此自动认定业务环境为生产；环境标签由用户明确填写。

## Thin / Thick

Thin 无需额外 Oracle Client，官方支持 Oracle Database 12.1 及以上，适合当前 19c 环境。
若服务器要求原生网络加密或账号密码验证方式不受 Thin 支持，可显式配置 Thick。
ACE 的 Thick 要求 Oracle Client 19 或更新版本，以保证支持调用超时；客户端位数需与 Python 一致。
Oracle Client 19 与 11.2 及以上数据库兼容，较新客户端的最低服务端版本可能不同，应按实际版本选择。

python-oracledb 的模式属于整个进程，不属于单条连接。同一 ACE 进程内所有 Oracle 数据源须使用
相同模式和客户端目录；切换时重启 ACE/Worker。系统不会在失败后自动切换模式或连接另一数据库。
SQL Server 连接不受 Oracle 模式限制。测试连接也会初始化进程模式。

依据：[驱动兼容说明](https://python-oracledb.readthedocs.io/en/v3.4.2/user_guide/installation.html)、
[初始化规则](https://python-oracledb.readthedocs.io/en/v3.4.2/user_guide/initialization.html)。

## 查询与限制

- 两套工作流复用项目数据源授权、默认路由、配置快照和来源审计。
- Oracle 19c 的参数不接受布尔值；应按真实字段传入数字或字符，不自动猜测 Y/N、0/1 等业务编码。
- 模型使用 Oracle SQL、`:name` 参数、ROWNUM、USER_TAB_COLUMNS/ALL_TAB_COLUMNS；不得混用 TOP、@name 或 ?。
- 执行前进行共享只读校验，再开启 `SET TRANSACTION READ ONLY`；拒绝写语句、锁定查询、序列、明显系统包调用和数据库链接。
- 使用只读最小权限账号；只读事务与 SQL 检查不能替代数据库权限，账号不应具有可产生外部副作用的自定义函数执行权限。
- 业务结果按已有行数限制读取，额外一行用于判断截断。敏感列直接遮蔽；CLOB 最多读取 501 字符后截断展示，BLOB/BFILE 仅报告大小。
- 连接参数限制 TCP 建连等待且不自动重试；`call_timeout` 限制单次数据库往返，不承诺整个分析或多次 LOB 读取的绝对总时长。
- 配置 Owner 是模型查询上下文，模型应核实对象并显式限定 Owner；本版不自动 ALTER SESSION 切换 Schema。
- 错误仅输出 Oracle/驱动错误码及固定建议，避免原始错误夹带密码、SQL 或业务值。

当前不提供数据库写入、Oracle 钱包管理或跨数据库事务。自动测试使用模拟连接；真实连通性需配置后测试。
