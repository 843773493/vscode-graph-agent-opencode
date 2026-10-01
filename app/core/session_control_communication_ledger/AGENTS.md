# 目录用途

`app/core/session_control_communication_ledger/` 承载 per-session `session-control.sqlite` 中跨 Session 通信 ledger 一条垂直链路的唯一实现：`communication_outbox` / `communication_inbox` 两张表的 DDL 与 target_accepted 覆盖索引、行投影、source 侧 create-or-get outbox（operation 层 PK 幂等 + communication 层 UNIQUE dedupe）与前向状态 CAS 迁移、target 侧 create-or-get inbox（main binding fresh 校验、admission identity 确定性派生）、admission 领取、execution bound、失败记录、状态索引列举与单条读取，以及 kind=reply 的双端因果证明。

# 可修改内容

- 可以维护 `communication_ledger.py` 中 `CommunicationLedgerMixin` 的方法族（`create_or_get_communication_outbox`、`advance_communication_outbox_state`、`create_or_get_communication_inbox`、`claim_communication_inbox_admission`、`mark_communication_inbox_execution_bound`、`record_communication_inbox_admission_failure`、`list_target_accepted_communication_inboxes`、`get_communication_inbox`、`_ensure_outbox_reply_direction`、`_ensure_inbox_reply_direction`）。
- 可以维护本模块自有的两张表 DDL、索引常量、列清单、`CommunicationOutboxRecord` / `CommunicationInboxRecord` 投影、`derive_communication_admission_identity` 派生器与取行 helper。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/core/test_session_control_store.py` 的 D5 节。

# 不可修改内容

- 不得在本目录实现 thread catalog/fence、creation record、execution intent、operation lease 或 thread owner binding 等其它控制库职责。
- 不得保留 `app/core/session_control_store.py` 的转发 shim、兼容别名或双套实现；公开符号必须只在本模块定义一处。
- 不得把 `SHA256_HEX_PATTERN`、`EXECUTION_BINDING_ID_PATTERN`、`EXECUTION_JOB_ID_PATTERN` 或 `validate_claim_fields` 在本目录复制一份；跨子包共用的形态口径只在 `session_control_primitives.py` 单点定义。
- 不得放宽幂等守卫：同 `send_operation_id` 或同 `communication_id` 的身份字段漂移必须 fail closed，不得覆盖、重基或静默吸收。
- 不得按墙钟时间自动让 inbox admission 过期；claim owner/generation 是无 TTL 的可恢复领取字段。
- 本子包不做 owner 准入门禁（不读 lifecycle fence）：`fence=deleting` 的 session 仍可写入 outbox/inbox。这是有意设计——准入红线由 `SessionLifecycleGate` 单点负责（唯一两个生产调用方 `CommunicationLedgerService.admit_outgoing_send` / `accept_incoming_send` 都在 `gate.exclusive(session_id)` 内）；绕过 gate 直接调用 store 即视为绕过。不得为此在 store 层补 fence 检查，这会与「gate 单点负责」的既有分工重复。

# 规范

- `CommunicationLedgerMixin` 只依赖宿主类提供的 `database_path`、`_connection`、`_ensure_open()` 与 `_write_transaction()`；不得假设 `SessionControlStore` 的其它方法存在。
- source/target 方向证明必须做在写事务内：`kind=reply` 时各自在本库对方表里要求方向相反的那一行，缺失或方向相同一律 fail closed。
- 一律通过本模块的取行 helper 读投影行，不得就地手写新的 SELECT；跨子包共用的形态校验统一从 `session_control_primitives` 导入。
- 错误分类沿用 `session_control_store`：`KeyError` 目标行缺失、`RuntimeError` 库被外部改动或 CAS 冲突、`ValueError` 输入形态非法、`TypeError` 输入类型错误。
- 修改本目录后运行 `uv run ruff check` 与 `uv run pytest tests/unit/core/test_session_control_store.py`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
