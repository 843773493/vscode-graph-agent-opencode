# 目录用途

存放附件目录存储 owner 的定向单元测试，按 catalog、原生访问及准入职责组织。

# 可修改内容

- 附件目录准入与恢复、worker 结算、holder 和取消语义的测试与 fixture。

# 不可修改内容

- 不实现生产存储逻辑，不用本地 helper 验收代替产品执行、fork 或恢复验收。

# 规范

- 目录状态由原上传入口生成，使用测试隔离工作区；所有测试保留进程外超时及内存保护。
- 外部线程屏障必须在 finally 释放并排空任务，不留下锁外写入或后台访问。
- GC 用原上传和冻结 claim 生成局部输入，核对 worker、取消、原 holder、账本先于 tombstone 及失败后的同入口重试；产品执行与硬退出验收仍归集成测试。
- 同目录 fork 分别核 prepared/reserved、committed 活动关联与 runtime 释放后的回收；跨工作区来源在原 export release 前持续保活，不由测试直接写 claim 行。
