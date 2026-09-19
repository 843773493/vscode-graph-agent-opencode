# 目录用途

存放跨 Session 通信（send/read/wait）的 typed admission/wait 合同层：canonical source/target main binding、communication 幂等冲突裁决、wait selector 与可恢复 DurableDeadline。对应 OpenSpec add-context-injection-lifecycle 任务 4.7（E03）。

# 可修改内容

- 通信地址、payload、outbox/inbox 只读视图等 typed 值对象。
- admission 幂等决策、reply 因果方向校验等纯合同函数。
- wait selector、DurableDeadline 冻结/恢复预算、状态聚合与 deadline 竞态裁决。

# 不可修改内容

- 不直接读写 SQLite/JSONL 或建立第二 ContextStore writer；持久化由后续 ledger/InboxAdmissionWorker owner 接线。
- 不 import CSM、不注入 canonical item、不唤醒目标 execution。
- 不引入 simulate_user 或旧 monitor_session_agent_end 语义。

# 规范

- 所有值对象必须 frozen 且字段闭合校验；违反合同抛出带闭集错误码的 CommunicationContractError。
- 幂等键只使用 (source GlobalThreadAddress, send_operation_id) 与 (source GlobalThreadAddress, communication_id) 两层，不引入别的别名。
- 复用 app/core/session_catalog_store.py 的 canonical session/thread 校验，不重复实现 ID 形态校验。
