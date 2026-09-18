# R21：catalog resolver 读取已发布 child thread

## 变更

- 在 `SessionControlStore` 增加 `get_published_child_thread_locator`，把
  `thread_catalog` 的 child 可见性与同一发布事务冻结的
  `thread_creation_records.final_relative_locator` 做一致性校验。
- `SessionCatalogPathResolver.resolve_thread_node` 的非 main 分支改为读取
  owner Session 的 `session-control.sqlite`，按冻结 locator 定位物理目录。
  解析过程不再扫描 `threads/`、按日期桶猜测路径或吸收未登记目录。
- 增加已发布 child、缺少 control 数据、未登记目录和已发布目录缺失等单测。

## 验证

- `uv run pytest tests/unit/core/test_session_catalog_resolver.py tests/unit/core/test_session_control_store.py -q --tb=short`
  - 144 passed
- `uv run pytest tests/unit/core/ -q --tb=short`
  - 678 passed
- `uv run ruff check app/core/session_control_store.py app/core/session_catalog_resolver.py tests/unit/core/test_session_catalog_resolver.py`
  - All checks passed
- `uv run python -m compileall -q app/core tests/unit/core/test_session_catalog_resolver.py`
  - exit 0

## 边界

R21 只打通 child thread 的权威定位读取，不接入 delegate 调用方、Job
execution、真实 ContextStore/rollout 文件或 Web 面板；这些仍属于后续 8.5/8.3
切片。旧 `SessionPathResolver` 未修改。
