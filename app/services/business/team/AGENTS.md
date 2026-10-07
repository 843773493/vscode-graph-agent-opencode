# 目录用途

存放同一 Session 内团队协作领域服务，负责 main/child thread 成员关系、任务面板权限、任务状态流转和任务分派。

# 可修改内容

- 团队成员和任务的业务规则。
- 团队任务分派、成员激活与结果通知流程。

# 不可修改内容

- 不直接实现文件持久化细节。
- 不定义 Agent 工具 schema 或 FastAPI 路由。

# 规范

- 成员必须归属同一 Session 的精确 thread；不得跨 Session attach 或共享团队状态，也不得伪造 delegated 来源。
- child catalog、creation record、成员 publication 与初始 scheduling intent 复用同一 Session SQLite owner 的发布事务；任务面板不得保存第二份 readiness 或 activation 权威。
- 只有协调者能增加成员和分派任务；普通成员只能更新分配给自己的任务。
- 任务与成员通知通过精确 thread 的 source/wakeup 编排接口；只提交 pending 来源和调度事实，不创建真实用户 Turn。失败必须写入任务或成员状态并抛错。
