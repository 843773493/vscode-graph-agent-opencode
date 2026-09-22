# 目录用途

`app/gateway/control/gateway_config_source/` 承载 Gateway 控制面中 config source layer 与 source journal 一条垂直链路的唯一实现：`config_source_layers` 权威 layer 行的去重同步与 CAS、`config_source_journal` 事件账本、`config_source_owner` generation 水位，以及 `source_generation_high_water_mark` 只读水位查询。

# 可修改内容

- 可以维护 `gateway_config_source.py` 中 `GatewayConfigSourceMixin` 的方法族（`get_source_layer`、`sync_config_source`、`_append_config_source_journal_in_connection`、`append_config_source_journal`、`_source_journal_from_row`、`_latest_source_journal_row`、`source_generation_high_water_mark`）。
- 可以维护本族 layer 行投影、presence 取值域校验与 revision/digest/generation CAS 语义。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/gateway/`、`tests/unit/services/infrastructure/` 与 `tests/contracts/api/` 下。

# 不可修改内容

- 不得在本目录实现 active snapshot、pending candidate、config apply claim/journal、runtime generation、restart intent、config event 或 workspace registry 等其它控制面职责。
- 不得保留 `app/gateway/control/gateway_state.py` 的转发 shim、兼容别名或双套实现；`GatewayStateStore` 只通过多继承装配本 mixin，方法体必须只在本模块定义一处。
- 不得把 `gateway_config` 表本身的通用读写放在本目录：本族只在 `sync_config_source` 内联动 `gateway_config` 的同一 config_key 行，通用读写仍属宿主。
- 不得把 `schema_version`、重载策略或 workspace 侧 `WorkspaceStateStore` 的 fanout 账本搬入本模块。

# 规范

- `GatewayConfigSourceMixin` 只依赖宿主类提供的 `_database`；不得假设 `GatewayStateStore` 的其它方法存在。
- 本族三张表 `config_source_layers`、`config_source_owner`、`config_source_journal` 的 DDL 仍由宿主 `_GATEWAY_MIGRATIONS` 装配，本模块只承载读写方法族，不重复定义建表语句。
- 必须与 `storage.py` 的边界一致：`storage.py` 只提供文件级原子 JSON 原语，不承载 layer/journal 状态；本模块不得绕过 SQLite 改用文件写入。
- 必须与 `config.py` 的边界一致：`config.py` 只作为调用方消费 source layer 与 journal，不得在本模块实现配置生效事务或文件监听。
- 错误分类沿用 `gateway_state` 约定：`ValueError` 输入形态非法、`ConfigConflictError` revision/digest/generation CAS 冲突、`RuntimeError` 事务后读取失败。
- 修改本目录后运行 `uv run ruff check` 与 `uv run pytest tests/unit/gateway/`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
