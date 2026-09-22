# 目录用途

`app/core/session_control_thread_owner_binding/` 承载 per-session `session-control.sqlite` 中 thread owner binding 字段槽一条垂直链路的唯一实现：`thread_owner_bindings` 行投影、canonical JSON 列表槽解析、2.1 负面合同校验、create-or-get/读取/typed 更新（含 revision CAS 与 append），以及该表行插入的唯一 SQL 实现。

# 可修改内容

- 可以维护 `thread_owner_binding.py` 中 `ThreadOwnerBindingMixin` 的方法族（`_insert_thread_owner_binding_row`、`ensure_thread_owner_binding`、`get_thread_owner_binding`、`update_thread_owner_binding`）。
- 可以维护本模块自有的 `thread_owner_bindings` DDL、`ThreadOwnerBinding` 投影、`_OWNER_BINDING_EPOCH_REASONS`、`_CREDENTIAL_KEY_PATTERN`，以及 `_validate_ref_text`/`_validate_json_entry`/`_canonical_json_list` 三个仅服务本族的校验器。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/core/` 与 `tests/unit/services/infrastructure/rollout_context/` 下。

# 不可修改内容

- 不得在本目录实现 thread catalog/fence、creation record、execution intent、operation lease 或跨 Session 通信账本等其它控制库职责。
- 不得保留 `app/core/session_control_store.py` 的转发 shim、兼容别名或双套实现；公开符号必须只在本模块定义一处。
- 不得在别处复制 `thread_owner_bindings` 的插入 SQL 行形状；发布事务与 `ensure_thread_owner_binding` 必须共用 `_insert_thread_owner_binding_row`。
- 不得把 owner 侧记录槽当作权威解释：prefix epoch 与 ToolSet revision 的语义仍属 ContextStore/ToolSet domain owner，本表不构成第二 writer。

# 规范

- `ThreadOwnerBindingMixin` 只依赖宿主类提供的 `database_path`、`_connection`、`_ensure_open()` 与 `_write_transaction()`；不得假设 `SessionControlStore` 的其它方法存在。
- `SHA256_HEX_PATTERN` 是 session-control 各族的共同形态约束（owner binding stable prefix、lease preimage、artifact manifest、通信 payload），当前唯一实现暂放本模块；若后续拆出 operation lease 或通信账本，应把它提升到中立位置（例如 `app/core/session_control_primitives.py`），并把两处子包一起改到新位置，禁止复制正则。
- 引用槽与列表槽必须走 2.1 负面合同：拒绝绝对路径、相对路径片段、NUL 与凭据形态键名。
- 错误分类沿用 `session_control_store`：`KeyError` 目标行缺失、`RuntimeError` 库被外部改动或 CAS 冲突、`ValueError` 输入形态非法、`TypeError` 输入类型错误。
- 修改本目录后运行 `uv run ruff check` 与 `uv run pytest tests/unit/core/test_session_control_store.py`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
