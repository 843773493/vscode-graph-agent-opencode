# 目录用途

本目录维护 Job 状态规则、精确 Session/thread 调度队列和待处理请求业务状态。

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
- 持久化能力通过 abstractions 协议注入。
- 所有队列状态变更必须在调度锁内完成。
- 恢复区分装载与完成：装载后不得重读覆盖内存队列，落盘和各 thread 派发完成后才登记恢复完成。取队首、落盘及创建任务按精确 thread 串行；硬错误重试沿用原活动槽和 Job，不能重放已启动 sibling。
- 没有运行任务体的取消共用收尾：canonical 取消成功后才发布内存终态、事件并唤醒原 thread 的 FIFO；落盘硬错误保留 cancelling 和原活动槽，允许原 Job 重试。
- 没有运行任务体的取消共用收尾：canonical 取消成功后才发布内存终态、事件并唤醒原 thread 的 FIFO；落盘硬错误保留 cancelling 和原活动槽，允许原 Job 重试。
