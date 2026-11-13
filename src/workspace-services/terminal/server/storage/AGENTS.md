# 目录用途

终端状态 SQLite 的独立写入线程、唯一 schema 和显式旧 JSON 导入入口。

# 可修改内容

- SQLite 事务读写、身份与 schema 核验，以及旧 schema 3 JSON 的一次性导入。
- 写入线程与 `../terminalStateStore.js` 之间的消息交接。

# 不可修改内容

- 不接管 PTY、领域资源 owner、HTTP 或 Manager 的批次确认队列。
- 不在普通请求中自动导入 JSON、扫描历史、VACUUM 或建立第二份持久状态。

# 规范

- 数据库 I/O 和 JSON 编解码只在 worker 执行；schema SQL 只有一份。
- 每批事务成功后才确认。失败回滚并保留原错误；回滚再次失败须同时报告。
- 导入要求停用原终端服务，目标库必须不存在，原 JSON 保持不变；原子发布不得覆盖目标。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
