# 目录用途

`app/services/infrastructure/workspace_activity/` 承载工作区状态库 `workspace.sqlite`
中 `workspace_activity` 这一条垂直链路的唯一实现：活动事件行投影、按 event_id 去重的
追加、游标分页读取、保留窗口边界与按 `occurred_at` 裁剪，以及活动事件实时订阅与
重放的 `WorkspaceActivityService`。

# 可修改内容

- 可以维护 `workspace_activity.py` 中 `WorkspaceActivityMixin` 的方法族
  （`append_activity`、`list_activity`、`activity_bounds`、`prune_activity`）。
- 可以维护本模块自有的 `WorkspaceActivityRecord` 投影、`_activity_record_from_row`
  行工厂、`WorkspaceActivityCursorGoneError` 与 `WorkspaceActivityService`。
- 可以维护对应的单元测试；测试放在 `tests/unit/services/infrastructure/`。

# 不可修改内容

- 不得在本目录实现配置来源层、配置 apply 账本、配置事件 outbox/relay 或
  `workspace_config` 读写等其它族；这些族仍属
  `app/services/infrastructure/workspace_state_store.py` 与
  `app/services/infrastructure/config/`。
- 不得保留 `workspace_state_store.py` 中的转发 shim、兼容别名或双套实现；
  `WorkspaceStateStore` 只按公开符号面从本模块导入，方法体不得在本目录之外复制。
- 不得在别处复制 `workspace_activity` 的行投影构造；追加去重回读与分页读取必须
  共用 `_activity_record_from_row`。
- 不得让本模块依赖 `app/services/infrastructure/config/**` 或 `config_service.py`；
  本族与配置族的边界是「活动事件表 vs 配置状态表」，两者只在宿主类上共存。

# 规范

- `WorkspaceActivityMixin` 只依赖宿主类提供的 `_database`（`SQLiteStateDatabase`）；
  不得假设 `WorkspaceStateStore` 的其它族方法存在。
- `WorkspaceActivityService` 需要构造宿主状态库，为避免宿主模块与本模块的导入环，
  在 `__init__` 内局部导入 `WorkspaceStateStore`；不得改成模块级导入。
- 错误分类沿用 `workspace_state_store` 约定：`ValueError` 输入参数非法、
  `WorkspaceActivityCursorGoneError` 游标已不在保留窗口内、`RuntimeError` 库被外部
  改动或订阅者消费速度不足。
- 分页读取与去重回读必须走本地 SQLite，不得静默吞掉错误或返回伪造默认值。
- 修改本目录后运行 `uv run ruff check` 与
  `uv run pytest tests/unit/services/infrastructure/test_workspace_activity_service.py
  tests/unit/services/infrastructure/test_workspace_state_store.py`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
