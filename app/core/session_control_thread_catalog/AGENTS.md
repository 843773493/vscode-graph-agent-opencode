# 目录用途

`app/core/session_control_thread_catalog/` 承载 per-session `session-control.sqlite` 控制库中 thread catalog 与 lifecycle fence 一条垂直链路的唯一实现：`thread_catalog` 的 main/child 权威指针、`lifecycle_fence` 生命周期闸门、已发布 child thread 的冻结 locator 解析，以及 `thread_catalog` 的 v1→v2 schema 加法升级。

# 可修改内容

- 可以维护 `thread_catalog.py` 中 `ThreadCatalogMixin` 的 thread catalog/fence 方法族（`_upgrade_thread_catalog_kind_v1_to_v2`、`initialize_main_thread`、`initialize_fence`、`cas_fence_transition`、`get_main_thread`、`get_fence`、`verify_matches_catalog_main_thread`、`get_published_child_thread_locator`、`get_thread_catalog_revision`、`list_child_thread_rows`）。
- 可以维护本模块自有的 `thread_catalog`/`lifecycle_fence` DDL 常量、`ChildThreadRow` 投影与 `validate_thread_relative_locator` 校验器。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/core/` 下。

# 不可修改内容

- 不得在本目录实现 creation record、execution intent、operation lease、thread owner binding 或跨 Session 通信账本等其它控制库职责。
- 不得保留 `app/core/session_control_store.py` 的转发 shim、兼容别名或双套实现；公开符号必须只在本模块定义一处。
- 不得把工作区级会话位置/父子权威索引（`session-catalog-index.json`）的解析搬入本目录，也不得在此扫描物理目录树。
- 不得静默吞掉不一致：库被外部改动、main row 缺失或多行、懒等冲突必须直接抛错。

# 规范

- `ThreadCatalogMixin` 只依赖宿主类提供的 `database_path`、`_connection` 与 `_ensure_open()`；不得假设 `SessionControlStore` 的其它方法存在。
- `thread_catalog` 只保存与工作区 catalog 冻结的 `main_thread_id` 匹配的 per-session 指针，不维护第二套会话层级。
- child thread 的物理定位必须继续读取同一发布事务冻结的 `thread_creation_records`，交叉校验失败即 fail closed。
- 错误分类沿用 `session_control_store`：`KeyError` 目标行缺失、`RuntimeError` 库被外部改动或语义冲突、`ValueError` 输入形态非法、`TypeError` 输入类型错误。
- 修改本目录后运行 `uv run ruff check` 与 `uv run pytest tests/unit/core/test_session_control_store.py`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
