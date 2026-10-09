# 目录用途

存放同一Session团队任务面板的存储访问及冻结状态纯投影。board、task、event、membership和fanout事实统一由SessionControlStore的collaboration ledger持久化；`activation.py`从已冻结团队正文生成membership/tasks两个facet。

# 可修改内容

- 团队存储访问、序列化以及冻结membership/tasks的纯投影。
- 同一Session ledger的读取适配与对应测试，不另持JSON快照。

# 不可修改内容

- 不在本目录实现第二ledger、fanout worker、Registry发布或Agent调度。
- 不写canonical context，不把任务面板投影作为成员publication/readiness权威。
- 不恢复旧`teams/` JSON快照、事件文件、跨Session成员兼容或双读。

# 规范

- 先由权威catalog resolver取得所属Session的SessionControlStore，不拼接固定会话目录。
- 业务mutation与fanout记录走同一ledger事务，禁止persist board后单独提交事件。
- activation纯投影只消费冻结输入，不读当前文件、ledger、Registry或调用模型。
- 非法身份、版本及损坏数据明确报错，不返回伪造空面板；旧开发数据可清理，当前格式仍需完整验证。
