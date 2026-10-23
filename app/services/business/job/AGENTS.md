# 目录用途

本目录维护 Job 状态规则、精确 Session/thread 调度队列和待处理请求业务状态。

## 职责树与调用链

```text
Job业务
├── 执行与状态
│   ├── service.py                    Job准入、执行调度与生命周期协调
│   ├── runtime_state.py              单个Job的运行时状态与执行句柄
│   └── lifecycle.py                  状态转换规则与允许状态集合
├── 用户控制
│   └── control_service.py            pause/resume/cancel与活动槽收尾
└── 待处理消息
    ├── pending_request_service.py     查询、修改、持久化协调入口
    ├── pending_request_controller.py  DTO投影及队列状态操作
    └── pending_queue.py               FIFO顺序、投递策略和快照版本
```

执行链从 `service.py` 开始，队列状态转换统一经过 `lifecycle.py`；用户控制由
`control_service.py` 协调执行任务和活动槽。待处理消息由
`pending_request_service.py` 串联 `pending_request_controller.py` 与队列，持久化仍通过
注入的 store 协议完成。队列按 `(session_id, thread_id)` 保持各 thread 的 FIFO，Session
级查询、删除与清空再聚合全部 thread。

# 可修改内容

- Job 状态模型与调度规则。
- Steering、Queued、取消和立即发送的业务语义。
- 待处理请求的持久化协调逻辑。

# 不可修改内容

- 不在此处实现 HTTP API、页面状态或 Gateway 路由。
- 不直接依赖 infrastructure 层的具体实现。

# 规范

- 同一 `(session_id, thread_id)` 最多只能有一个 active Job；同 Session 的不同 thread 可以并行，parent 等待 child 不得占用 child 的执行槽。
- 队列、active 槽、幂等准入和恢复保持精确 thread 归属；会话查询、删除和排空仍聚合该 Session 的全部 thread。
- 队列快照版本只由 `JobPendingQueue` 按 Session 保存；`QueueEntry` 只承载单条消息状态，位置理由只在对应 thread 队首变动时更新。
- 持久化能力通过 abstractions 协议注入。
- 所有队列状态变更必须在调度锁内完成。
- bootstrap 的待处理摘要由外层 Session 读准入保护，只读摘要不再通过新消息 preparation 检查 active；已准入历史允许在 catalog deleting 后、原 guard 释放前完成。
- 恢复始终刷新 SQLite 权威队列的内存投影，不能用已装载标志忽略新 acceptance；刷新不得替换原 active Job、claim 或 task。取队首、落盘及创建任务按精确 thread 串行；硬错误重试沿用原活动槽和 Job，不能重放已启动 sibling。
- 没有运行任务体的取消共用收尾：canonical 取消成功后才发布内存终态、事件并唤醒原 thread 的 FIFO；落盘硬错误保留 cancelling 和原活动槽，允许原 Job 重试。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。

- 活任务取消终态写入失败保留 cancelling、原 task 错误与活动槽；控制/排空透传错误，重试沿原 Job 收尾。批量排空先结算其它项及快照，再报告错误；未提交终态不唤醒 FIFO。
