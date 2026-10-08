# 目录用途

存放跨 Session 通信（send/read/wait）的业务准入与观察 owner：canonical source/target main binding、outbox/inbox 幂等与因果裁决、冻结消费投影、wait selector 与可恢复 DurableDeadline。对应 OpenSpec add-context-injection-lifecycle 任务 4.7（E03）。

# 可修改内容

- 通信地址、payload、outbox/inbox 只读视图等 typed 值对象。
- admission 幂等决策、reply 因果方向校验，以及经唯一 SessionControlStore port 提交的通信 ledger 状态转换。
- 通信 ledger 的 claim、恢复与 binding CAS 合同；唯一 InboxAdmissionWorker 及必填 typed binding port 的实际委托接线归 orchestration/communication，由 backend 组合根注入。
- wait selector、DurableDeadline 冻结/恢复预算、状态聚合与 deadline 竞态裁决。

# 不可修改内容

- 不直接拼写 SQLite/JSONL，不建立第二通信账本或 ContextStore writer；持久化复用各自现有 owner。
- 不 import CSM 或具体 runtime/source/Job 编排实现，不直接追加 canonical item；binding port 委托唯一 source+wakeup/admission owner。
- 不在本目录做应用装配、创建网络 channel 或注册 backend lifespan；acceptance/startup 驱动同一 worker 的接线归组合根。
- 不引入 simulate_user 或旧 monitor_session_agent_end 语义。

# 规范

- worker 以短 claim → 释放 gate → 委托 binding → 幂等 CAS 收口；不得在 gate 内重入同一准入 owner，source 与 target gate 不得重叠。read/wait 只冷读，不负责唤醒 worker 或目标 runtime。
- 所有值对象必须 frozen 且字段闭合校验；违反合同抛出带闭集错误码的 CommunicationContractError。
- 幂等键只使用 (source GlobalThreadAddress, send_operation_id) 与 (source GlobalThreadAddress, communication_id) 两层，不引入别的别名。
- 复用 app/core/session_catalog_store.py 的 canonical session/thread 校验，不重复实现 ID 形态校验。
