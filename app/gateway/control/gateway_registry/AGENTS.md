# 目录用途

`app/gateway/control/gateway_registry/` 承载 Gateway 控制面中 workspace registry 与 registry apply journal 一条垂直链路的唯一实现：`gateway_workspace_registry` 权威注册行的读取与批处理替换、`registry_meta` 中 `workspace` revision 的读取与 CAS、`registry_apply_journal` 批处理恢复账本，以及把受控启动自身产生的 registry 提交纳入配置 apply 基线的 `rebase_config_apply_registry_revision`。

# 可修改内容

- 可以维护 `gateway_registry.py` 中 `GatewayRegistryMixin` 的方法族（`load_workspace_registry`、`get_registry_revision`、`rebase_config_apply_registry_revision`、`list_registry_apply_journal`、`recover_registry_apply_journal`、`replace_workspace_registry`、`_start_registry_apply_journal`、`_finish_registry_apply_journal`）。
- 可以维护本族内 `replace_workspace_registry` 的 owner 作用域校验、target identity 去重与 stale 行删除逻辑。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/gateway/` 下。

# 不可修改内容

- 不得在本目录实现 config source、active snapshot、config apply journal、runtime generation、restart intent 或 config event 等其它控制面职责。
- 不得保留 `app/gateway/control/gateway_state.py` 的转发 shim、兼容别名或双套实现；`GatewayStateStore` 只通过多继承装配本 mixin，方法体必须只在本模块定义一处。
- 不得复制 `registry_apply_journal` 与 `gateway_workspace_registry` 的写入 SQL 到宿主或其它族；批处理的 start/finish/replace 必须共用本模块私有方法。
- 不得把 registry 批处理越权改写为静默吸收：跨 target owner 修改必须继续抛 `PermissionError`，revision CAS 冲突必须抛 `ConfigConflictError`。

# 规范

- `GatewayRegistryMixin` 只依赖宿主类提供的 `_database` 与 `get_config`；不得假设 `GatewayStateStore` 的其它方法存在。
- 本族三张表 `gateway_workspace_registry`、`registry_meta`、`registry_apply_journal` 的 DDL 仍由宿主 `_GATEWAY_MIGRATIONS` 装配，本模块只承载读写方法族，不重复定义建表语句。
- `workspace_registry_meta`（`gateway_config` 行）是本族读写的元数据载体，语义上属于本族；`gateway_config` 表本身的通用读写仍属宿主。
- 必须与 `storage.py` 的边界一致：`storage.py` 只提供 `read_json_object` / `atomic_write_json` 文件级原子 JSON 原语，不承载 registry 业务；本模块不得绕过 SQLite 改用文件读写注册表。
- 必须与 `config.py` 的边界一致：`config.py` 只作为调用方消费 registry revision 与重启流程，不得在本模块实现 config 生效事务。
- 错误分类沿用 `gateway_state` 约定：`ValueError` 输入形态非法、`PermissionError` 跨 owner 越权、`ConfigConflictError` CAS/并发冲突。
- 修改本目录后运行 `uv run ruff check` 与 `uv run pytest tests/unit/gateway/test_gateway_state.py`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
