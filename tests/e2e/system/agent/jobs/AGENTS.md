# 目录用途

存放 Job 调度、排队、并发、事件与中断语义的端到端测试。

## 职责树

```text
jobs/
├── test_job_scheduling.py   同 Session FIFO、跨 Session 并行、Job 事件历史
└── test_session_interrupt.py 真实 SSE 中断 text/tool 阶段并验证后续消息
```

按被验证的 Job 生命周期选文件：队列次序、并行 Session 与事件历史归入 `test_job_scheduling.py`；通过 Session trace SSE 在执行中打断并验证下一条消息的恢复归入 `test_session_interrupt.py`。

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
