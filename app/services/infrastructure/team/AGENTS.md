# 目录用途

存放同一Session团队任务面板的存储访问。board、task、event、membership和fanout事实统一由SessionControlStore的collaboration ledger持久化；冻结正文的membership/tasks语义派生归business/resource_derivations。

# 可修改内容

- 团队存储访问和序列化。
- 同一Session ledger的读取适配与对应测试，不另持JSON快照。

# 不可修改内容

- 不在本目录实现业务语义loader、第二ledger、fanout worker、Registry发布或Agent调度。
- 不写canonical context，不把任务面板投影作为成员publication/readiness权威。
- 不恢复旧`teams/` JSON快照、事件文件、跨Session成员兼容或双读。

# 规范

- 先由权威catalog resolver取得所属Session的SessionControlStore，不拼接固定会话目录。
- 业务mutation与fanout记录走同一ledger事务，禁止persist board后单独提交事件。
- 非法身份、版本及损坏数据明确报错，不返回伪造空面板；旧开发数据可清理，当前格式仍需完整验证。
- board、member、task与event按当前DTO严格解析；混入旧Session归属字段也必须明确拒绝，不静默丢弃或重写为child-thread格式。
