# ACE-RUNTIME-016：Claude大提示词安全传输

## 目标

修复异常流程在注入大型SQL Server Schema时触发Windows `[WinError 206]` 的问题，同时不裁剪模型
规则、不削弱数据库只读能力，并让同一Runtime修复覆盖开发和异常两套流程。

## 证据与根因

- 失败发生在Windows创建Claude Code进程时，程序和工作区均存在；
- 当前MES连接的只读Schema描述为26,583字符；
- 异常系统提示词为35,483字符，结构化输出Schema约3,501字符；
- 使用旧argv协议组装后的Windows命令行约39,977字符，超过32,767字符限制；
- `claude.exe`在解析参数前即无法启动，因此问题不是模型、API、SQL执行或项目长路径。

## 实现范围

- 系统提示词写入每轮独占UTF-8临时文件，使用`--append-system-prompt-file`传递；
- 用户消息使用`--input-format text`和stdin传递；
- 流式Popen和最终JSON subprocess.run路径共用相同协议；
- 临时文件在正常、失败、超时和中断后的作用域退出时清理；
- 使用`subprocess.list2cmdline`测量剩余Windows命令行，达到限制时在启动前明确拒绝；
- 日志只记录传输方式和各部分字符数，不记录提示词、消息或数据库结构正文；
- 增加超大提示词、临时文件生命周期、stdin、流式调用和长度预检测试。

## 不在本次范围

- 不改变SQL Server只读权限、60秒超时和返回行数上限；
- 不截断数据库Schema或项目知识；
- 不修改异常决策协议；
- 不在本次引入按需Schema语义检索。该优化应独立设计表/字段发现动作及回退策略后再实施。

## 验收

- 100KB以上系统提示词不出现在argv中，构造后的命令低于Windows限制；
- Claude Code从临时文件读取系统提示词，从stdin读取用户消息；
- 调用结束后临时文件不存在；
- 两套Runtime路径及所有非live测试、Ruff、compileall和diff检查通过；
- 发布`v0.7.3`并向GitHub推送一次。
