# 目录用途

存放 Job 调度、排队、并发、事件与中断语义的端到端测试。

# 可修改内容

- Job 状态机和调度 API 的 E2E 用例。
- pending request、事件流与用户中断行为断言。

# 不可修改内容

- 不放 Agent 工具细节或 Session 存储布局测试。
- 不通过进程内调用绕过真实 Job API。

# 规范

- 必须断言终态和关键事件，不得默默忽略失败 Job。
- 并发测试必须使用隔离 Session 并设置确定性等待条件。
- 正式产物写入与本目录测试路径对应的 `out/tests/e2e/system/agent/jobs/`。
