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

## 最终双模式验证与 fork import 7F 归因（收尾轮补记）

> 本节由收尾轮补记：R21 实施主体已在提交 23f6860 进入主干；本节数字为
> 2026-09-19 提交态（b09ba08）下经 R24 finalize 修复 7a50fef 引入的提交态
> 回归后的复测结果，日志见 artifacts/r24-finalize-*。

### fork import 7 failed 归因：既有深层债务，非 R21 迁移回归

test_rollout_fork_import_sources.py 的 7 failed（错误形如
`RuntimeError: full_rollout_copy message 没有对应 canonical item`）与
session ID 形态无关：

- catalog 模式 + canonical ID（artifacts/r21-a-final.log）：7 failed / 504 passed；
- 恢复原始 source/target ID 并以 legacy 模式运行
  （artifacts/r21-fork-import-legacy-original-ids.log）：同样 7 failed，
  错误完全相同（message_id 前缀随 ID 变化，canonical item 映射缺口不变）。

结论：fork full-copy remap 链路在把 source item 复制为 target item 后，
message 与 canonical item 的对应关系存在与 ID 无关的既有缺口。该家族在
R19 起即登记为既有债务（legacy 10F 基线内）；R21 不越权改生产 remap 代码，
7F 保留并移交后续 fork remap 专项轮。调试证据：
artifacts/r21-debug-plugin.py、r21-fork-debug-dump.json（dump 显示
message_map 有 root→fork-message 映射，但 new_items 与 messages 表的
canonical item 对应缺失）。

### 双模式全量数字（当前提交态基线）

- 默认 catalog 模式 tests/integration/backend/sessions：10 failed / 1146
  passed / 3 errors（r24-finalize-sessions-catalog-full.log；3E 为 fixture
  legacy 门控首版缺陷，已修正，修正后定向 3 passed）；
- legacy 模式（BOXTEAM_SESSION_CATALOG_RESOLVER=0）：9 failed / 1150
  passed / 0 errors（r24-finalize-sessions-legacy-full.log）；对照 R19 基线
  10F/1149P：fork_reader_locks 债务转绿，其余逐项一致，启动错误清零；
- 全量 tests/unit：4173 passed / 7 skipped / 0 failed
  （r24-finalize-unit-full.log）。

残余 9-10F 全部为 R19/R21 起登记的既有债务（fork import 家族、
itemized_migration_legacy、config_migration_session_retry、
session_generation_strategies 等），与 R21 变更无新增回归。

### 派生发现：静态 fixture 工作区现代化（移交后续轮）

tests/fixtures/workspaces/custom_tool_test_workspace 的物理布局已迁至日期桶 +
session-catalog.sqlite（未提交），但其 rollout SQLite 仍为 v1 格式：

- 提交态（未含该迁移）下 web 集成测试因 fixture 携带旧 JSON 权威索引、
  catalog 模式拒绝双读而后端启动即失败（r21-boundary-head.log）；
- 布局迁移后后端可启动，但读取 turn projection 报
  `schema-upgrade-required: current=1, target=4`（r21-boundary-diag.log），
  需经 legacy_import_v1_to_v2 显式迁移或按当前产品 writer 重新生成；
  fixture 中 ses_4c0a…2345 等 v1 源同时被 migration machinery 测试消费，
  迁移方案必须区分「v1 只读源」与「v2 runtime 消费」两类会话。

