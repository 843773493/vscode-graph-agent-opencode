# 目录用途

`app/services/infrastructure/workspace_config_events/` 承载工作区状态库 `workspace.sqlite`
中 config event outbox 与 consumer/relay 投递账本这一条垂直链路的唯一实现：
`config_events` 权威 outbox 的幂等 append、按域分页读取、cursor 边界与裁剪、单
consumer relay 状态机，以及 `config_event_relay_delivery` 中每个 consumer 独立的
claim / delivered / failed 投递状态。

# 可修改内容

- 可以维护 `workspace_config_events.py` 中 `WorkspaceConfigEventMixin` 的方法族
  （`_event_from_row`、`_select_config_event`、`_insert_config_event`、
  `append_config_event`、`list_config_events_for_relay`、
  `claim_config_events_for_consumer`、`mark_config_event_delivered_for_consumer`、
  `fail_config_event_for_consumer`、`claim_config_event_relay`、
  `mark_config_event_relay_delivered`、`fail_config_event_relay`、
  `list_config_events`、`config_event_bounds`、`ensure_config_event_cursor`、
  `prune_config_events`）。
- 可以维护本族的 `config_events` 行投影常量、consumer 投递状态机与 relay 租约/
  重试语义。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/services/infrastructure/` 下。

# 不可修改内容

- 不得在本目录实现 config source journal、active snapshot、pending candidate、
  config apply claim/journal、`workspace_config` 读写或 runtime generation 等其它族；
  这些族仍属 `app/services/infrastructure/workspace_state_store.py` 与
  `app/services/infrastructure/config/`。
- 不得保留 `workspace_state_store.py` 中的转发 shim、兼容别名或双套实现；
  `WorkspaceStateStore` 只通过多继承装配本 mixin，方法体必须只在本模块定义一处。
- 不得在其它族复制 `config_events` 的插入/幂等 SQL、24 列行投影或
  `config_event_relay_delivery` 的 claim SQL；跨族写入必须继续通过本模块的
  `_insert_config_event`。
- 不得把 consumer 投递状态与全局 relay 状态混为一谈：`config_event_relay_delivery`
  按 consumer 隔离，`config_events.relay_*` 是全局单 consumer outbox 语义。

# 规范

- `WorkspaceConfigEventMixin` 只依赖宿主类提供的 `_database`；不得假设
  `WorkspaceStateStore` 的其它族方法存在（唯一例外是其它族按约定回调本模块的
  `_insert_config_event`）。
- 本族两张表的 DDL 仍由宿主 `_WORKSPACE_MIGRATIONS` 装配，本模块只承载读写方法族，
  不重复定义建表语句；调整表结构须同步迁移序号与 `schema_version`。
- 必须与 `workspace_activity/` 的边界一致：活动事件表 vs 配置事件表，两者只在
  `WorkspaceStateStore` 上共存，互不引用对方的私有辅助。
- 必须与 `config/` 子包及 `config_service.py` 的边界一致：它们只作为调用方消费事件
  与游标，不得在本模块实现配置生效事务。
- 错误分类沿用 `workspace_state_store` 约定：`ValueError` 输入形态非法、
  `KeyError` 目标事件缺失、`ConfigConflictError` claim/幂等冲突、`RuntimeError`
  事务后读取失败、`ConfigEventCursorGoneError` 游标已被裁剪。
- 修改本目录后运行 `uv run ruff check` 与
  `uv run pytest tests/unit/services/infrastructure/test_workspace_state_store.py`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
