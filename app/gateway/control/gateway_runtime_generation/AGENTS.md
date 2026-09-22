# 目录用途

`app/gateway/control/gateway_runtime_generation/` 承载 Gateway 控制面中 runtime generation 生命周期一条垂直链路的唯一实现：`gateway_runtime_generation` 权威行的记录/读取/状态 CAS、serving handoff 的 fencing 校验与旧 generation 排空、失败回滚，以及幂等 close 与 health proof 投影。

# 可修改内容

- 可以维护 `gateway_runtime_generation.py` 中 `GatewayRuntimeGenerationMixin` 的方法族（`_runtime_generation_from_row`、`get_gateway_runtime_generation`、`record_gateway_runtime_generation`、`update_gateway_runtime_generation`、`active_gateway_runtime_generation`、`handoff_gateway_runtime_generation`、`rollback_gateway_runtime_handoff`、`close_gateway_runtime_generation`）。
- 可以维护本族 generation 行投影、listener/state 取值域校验与 fencing CAS 语义。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/gateway/` 下。

# 不可修改内容

- 不得在本目录实现 config source、active snapshot、pending candidate、config apply claim/journal、restart intent、config event 或 workspace registry 等其它控制面职责。
- 不得保留 `app/gateway/control/gateway_state.py` 的转发 shim、兼容别名或双套实现；`GatewayStateStore` 只通过多继承装配本 mixin，方法体必须只在本模块定义一处。
- 不得把 restart intent promotion 在 `promote_active_config_snapshot` 事务内联的 generation 更新搬到这里（那是跨族原子事务的一部分）；本模块只承载 generation 自身的独立生命周期操作。
- 不得把 fencing token 校验降级为「先读后写」的非原子检查：handoff/rollback/close 必须在 `BEGIN IMMEDIATE` 内完成 CAS。

# 规范

- `GatewayRuntimeGenerationMixin` 只依赖宿主类提供的 `_database`；不得假设 `GatewayStateStore` 的其它方法存在。
- 本族表 `gateway_runtime_generation` 的 DDL 仍由宿主 `_GATEWAY_MIGRATIONS` 装配，本模块只承载读写方法族，不重复定义建表语句。
- 必须与 `storage.py` 的边界一致：`storage.py` 只提供文件级原子 JSON 原语，不承载 generation 状态；本模块不得绕过 SQLite 改用文件写入。
- 必须与 `config.py` 的边界一致：`config.py` 只作为调用方消费 generation 与重启流程，不得在本模块实现配置生效事务。
- 错误分类沿用 `gateway_state` 约定：`ValueError` 输入形态非法、`ConfigConflictError` state/fencing CAS 冲突、`RuntimeError` 事务后读取失败。
- 修改本目录后运行 `uv run ruff check` 与 `uv run pytest tests/unit/gateway/test_gateway_state.py tests/unit/gateway/test_gateway_config.py`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
