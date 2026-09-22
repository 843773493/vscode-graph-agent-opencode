# 目录用途

`app/core/session_control_operation_lease/` 承载 per-session `session-control.sqlite` 中通用 operation lease 一条垂直链路的唯一实现：`session_operation_leases` 表 DDL 与非终态部分索引、行投影、create-or-get 幂等准入、fencing token CAS 链（settling/settle/takeover）与读取（get/find/list/verify token）。

# 可修改内容

- 可以维护 `operation_lease.py` 中 `OperationLeaseMixin` 的方法族（`create_or_get_lease`、`mark_lease_settling`、`settle_lease`、`takeover_lease`、`get_lease`、`find_lease_by_operation`、`list_non_terminal_leases`、`verify_lease_token` 与内部取行 `_lease_row_or_raise`）。
- 可以维护本模块自有的 `session_operation_leases` DDL、非终态部分索引常量与 `_lease_from_row` 投影。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/core/test_session_lifecycle_gate.py`。

# 不可修改内容

- 不得在本目录实现 thread catalog/fence、creation record、execution intent、thread owner binding 或跨 Session 通信账本等其它控制库职责。
- 不得保留 `app/core/session_control_store.py` 的转发 shim、兼容别名或双套实现；公开符号必须只在本模块定义一处。
- 不得按墙钟时间自动让 lease 到期，也不得把删除 drain 与恢复路径的消费范围从 `active|settling` 放宽。
- 不得把 `SessionOperationLease` 字段集与状态闭集复制到本目录；该 typed 合同仍由 `app/core/session_lifecycle_gate.py` 单点定义。

# 规范

- `OperationLeaseMixin` 只依赖宿主类提供的 `database_path`、`_connection`、`_ensure_open()` 与 `_write_transaction()`，以及 `thread_catalog` 的 `FENCE_ROW_ID`；不得假设 `SessionControlStore` 的其它方法存在。
- `create_or_get_lease` 按 `(captured_lifecycle_generation, operation_identity)` 幂等；同 identity 不同 preimage 必须 fail closed，不得覆盖或重基。
- fence 非 `active`（尤其 `deleting`）时拒绝建立新 lease；`expected_generation` 是 gate 内 fresh 校验之外的最后一道防线。
- 终态只能从 `settling` 进入；`takeover` 的源状态闭集保持 `active|settling`，接管后旧 token 的全部 callback 必须失败。
- 错误分类沿用 `session_control_store`：`KeyError` 目标行缺失、`RuntimeError` 库被外部改动或 CAS 冲突、`ValueError` 输入形态非法、`TypeError` 输入类型错误。
- 修改本目录后运行 `uv run ruff check` 与 `uv run pytest tests/unit/core/test_session_lifecycle_gate.py`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
