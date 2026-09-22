# 目录用途

`app/services/infrastructure/workspace_config_source/` 承载工作区状态库
`workspace.sqlite` 中 config source 一条垂直链路的唯一实现：`config_source_layers`
权威 layer 行（去重同步、revision/digest CAS、上一版 payload 与备份路径）、
`config_source_journal` 事件账本与 `config_source_owner` generation 水位，以及
`config_source_fanout` 逐 generation/逐 workspace 的导入结果账本。

# 可修改内容

- 可以维护 `workspace_config_source.py` 中 `WorkspaceConfigSourceMixin` 的方法族
  （`get_source_layer`、`sync_config_source`、`update_source_generation`、
  `_append_config_source_journal_in_connection`、`append_config_source_journal`、
  `list_config_source_journal`、`source_generation_high_water_mark`、
  `record_config_source_fanout`、`prepare_config_source_fanout`、
  `config_source_fanout_summary`、`list_config_source_fanout`）。
- 可以维护本族的 `config_source_journal` 行投影常量、行工厂、A-A 去重语义、
  generation CAS 与 fan-out 三态汇总。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/services/infrastructure/` 下。

# 不可修改内容

- 不得在本目录实现 active snapshot、pending candidate、config apply claim/journal、
  config event outbox/relay 或 `workspace_config` 读写等其它族；这些族分别属
  `workspace_config_events/` 与 `workspace_state_store.py`。
- 不得保留 `workspace_state_store.py` 中的转发 shim、兼容别名或双套实现；
  `WorkspaceStateStore` 只通过多继承装配本 mixin，方法体必须只在本模块定义一处。
- 不得在其它族复制 `config_source_journal` 的插入/幂等 SQL、行投影或
  `config_source_owner` 水位 SQL；跨族写入必须通过本模块的
  `_append_config_source_journal_in_connection`。
- 不得把 `config_source_layers` 的 writer 扩张到本目录之外：`WorkspaceSourceOwner`
  （`config/source_owner.py`）是另一张库的 owner，不是本表的第二 writer。

# 规范

- `WorkspaceConfigSourceMixin` 只依赖宿主类提供的 `_database`；不得假设
  `WorkspaceStateStore` 的其它族方法存在。
- 本族三张表的 DDL 仍由宿主 `_WORKSPACE_MIGRATIONS` 装配，本模块只承载读写方法族，
  不重复定义建表语句；调整表结构须同步迁移序号与 `schema_version`。
- `sync_config_source` 是 layer 行的唯一 writer，且必须在同一事务内完成
  layer 行 upsert 与 journal 追加，避免 source/journal 裂脑。
- 必须与 `config/` 子包及 `config_service.py` 的边界一致：它们只作为调用方消费
  layer 与 journal，不得在本模块实现配置生效事务。
- 错误分类沿用 `workspace_state_store` 约定：`ValueError` 输入形态非法、
  `ConfigConflictError` revision/digest/generation CAS 冲突、`RuntimeError`
  事务后读取失败。
- 修改本目录后运行 `uv run ruff check` 与
  `uv run pytest tests/unit/services/infrastructure/test_workspace_state_store.py`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
