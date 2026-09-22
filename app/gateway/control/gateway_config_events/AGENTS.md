# 目录用途

`app/gateway/control/gateway_config_events/` 承载 Gateway 控制面中 config event outbox 与 consumer/relay 投递账本一条垂直链路的唯一实现：`config_events` 权威 outbox 的幂等 append、分页读取、cursor 边界与裁剪，以及 `config_event_relay_delivery` 中每个 consumer 独立的 claim / delivered / failed 投递状态。

# 可修改内容

- 可以维护 `gateway_config_events.py` 中 `GatewayConfigEventMixin` 的方法族（`_event_from_row`、`_select_config_event`、`_insert_config_event`、`append_config_event`、`list_config_events_for_relay`、`claim_config_events_for_consumer`、`mark_config_event_delivered_for_consumer`、`fail_config_event_for_consumer`、`claim_config_event_relay`、`mark_config_event_relay_delivered`、`fail_config_event_relay`、`list_config_events`、`config_event_bounds`、`ensure_config_event_cursor`、`prune_config_events`）。
- 可以维护本族 `config_events` 行投影、consumer 投递状态机与 relay 租约/重试语义。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/gateway/` 下。

# 不可修改内容

- 不得在本目录实现 config source、active snapshot、pending candidate、config apply claim/journal、runtime generation、restart intent 或 workspace registry 等其它控制面职责。
- 不得保留 `app/gateway/control/gateway_state.py` 的转发 shim、兼容别名或双套实现；`GatewayStateStore` 只通过多继承装配本 mixin，方法体必须只在本模块定义一处。
- 不得在其它族复制 `config_events` 的插入/幂等 SQL 或 `config_event_relay_delivery` 的 claim SQL；跨族写入必须继续通过本模块的 `_insert_config_event`。
- 不得把 consumer 投递状态与全局 relay 状态混为一谈：`config_event_relay_delivery` 按 consumer 隔离，`config_events.relay_*` 是全局单 consumer outbox 语义。

# 规范

- `GatewayConfigEventMixin` 只依赖宿主类提供的 `_database`；不得假设 `GatewayStateStore` 的其它方法存在。
- 本族两张表 `config_events`、`config_event_relay_delivery` 的 DDL 仍由宿主 `_GATEWAY_MIGRATIONS` 装配，本模块只承载读写方法族，不重复定义建表语句。
- 必须与 `storage.py` 的边界一致：`storage.py` 只提供文件级原子 JSON 原语，不承载事件账本；本模块不得绕过 SQLite 改用文件写入事件。
- 必须与 `config.py` 的边界一致：`config.py` 与 `app/api/config.py` 只作为调用方消费事件与游标，不得在本模块实现配置生效事务。
- 错误分类沿用 `gateway_state` 约定：`ValueError` 输入形态非法、`KeyError` 目标事件缺失、`ConfigConflictError` claim/幂等冲突、`RuntimeError` 事务后读取失败、`ConfigEventCursorGoneError` 游标已被裁剪。
- 修改本目录后运行 `uv run ruff check` 与 `uv run pytest tests/unit/gateway/test_gateway_state.py`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
