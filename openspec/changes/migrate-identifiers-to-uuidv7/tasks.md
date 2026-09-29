## 1. 显式依赖与生成来源（D1）

- [ ] 1.1 在 `pyproject.toml` 的 `dependencies` 中显式新增 `uuid-utils>=0.16`，运行 `uv sync` 并确认 `uv.lock` 中 `uuid-utils` 仍是同一锁定版本 `0.16.0`。门槛：`uv sync` 退出码 0；`uv run python -c "import uuid_utils; print(uuid_utils.__version__)"` 退出码 0 且输出 `0.16.0`。
- [ ] 1.2 把 `app/core/identifier.py` 的 `create_uuid_hex()` 改为返回 `uuid_utils.uuid7().hex`；保留 `create_prefixed_id(prefix)` 的 `f"{prefix}_{hex}"` 外形。门槛：`uv run python -c "from app.core.identifier import create_uuid_hex; h=create_uuid_hex(); assert h[12]=='7' and h[16] in '89ab' and len(h)==32"` 退出码 0。
- [ ] 1.3 增加 fail-closed 守卫：`uuid_utils` 导入失败或 `uuid7` 不可用时抛出详细错误，MUST NOT 回退 `uuid.uuid4()`。门槛：单测模拟导入缺失，断言抛错且无 v4 产出（`uv run pytest -q tests/unit/core/test_identifier.py` 退出码 0）。

## 2. 单调性与时钟回拨（D2、D4c）

- [ ] 2.1 为唯一工厂补充同毫秒单调测试：同进程内同一毫秒连续生成 20000 个 id，断言按 hex 排序与生成顺序逐字节一致且全部唯一。门槛：`uv run pytest -q tests/unit/core/test_identifier_uuidv7_monotonic.py` 退出码 0。
- [ ] 2.2 补跨毫秒自然单调测试：连续生成 200000 个 id，断言全局有序且唯一。门槛：同一测试文件退出码 0。
- [ ] 2.3 实现并测试时钟回拨钳制：注入早于上次生成时刻的时间源时，新 id MUST 不小于上一次。门槛：单测断言回拨场景下非递减，退出码 0。
- [ ] 2.4 断言生成路径不传显式 `timestamp`（可静态检查或断言调用形态），并在代码注释中说明显式时间戳会破坏同毫秒单调。门槛：对应单测退出码 0。

## 3. 校验层正名与单一 profile（D5）

- [ ] 3.1 把 `app/core/session_catalog_store.py` 的 `_validate_uuid_v4_payload` 正名为 `_validate_uuid_payload`，`_UUID_VERSION_HEX_INDEX` 语义改为要求 `version == 7`；删除任何 `v4` 命名残留；更新 `validate_session_id`/`validate_thread_id` 的 docstring 与注释为「UUIDv7 位 profile」。门槛：`uv run python -c "import app.core.session_catalog_store as s; print([n for n in dir(s) if 'uuid' in n.lower()])"` 退出码 0 且输出无 v4 命名。
- [ ] 3.2 同步 `app/protocol/canonical.py` 的注释（现写「payload 第 13 个 hex 位为 4（UUIDv4 version）…」）。门槛：`rg -n 'UUIDv4|非 v4|v4 bit' app/core/session_catalog_store.py app/protocol/canonical.py` 退出码 1（0 命中）。
- [ ] 3.3 补负向测试：payload 第 13 个 hex 为 `4` 的 id MUST 被拒绝。门槛：`uv run pytest -q tests/unit/core/test_canonical_identifier_matrix.py` 退出码 0。
- [ ] 3.4 更新 `tests/unit/core/test_canonical_identifier_matrix.py` 的 docstring（现写「非 v4 bits」）与 `make_session_id`/`make_thread_id`（现用 `uuid.uuid4().hex`）为 v7 生成。门槛：`rg -n 'uuid4' tests/unit/core/test_canonical_identifier_matrix.py` 退出码 1。

## 4. 日期桶与 id 内嵌时间戳一致性（D4）

- [ ] 4.1 在 `validate_storage_relative_locator()` 中加入「`sessions/YYYY/MM/DD` 的 UTC 日期 == id 内嵌 48 bit 毫秒时间戳的 UTC 日期」断言；不一致抛显式完整性错误。门槛：`uv run pytest -q tests/unit/core/test_session_catalog_store.py` 退出码 0。
- [ ] 4.2 补负向测试：构造「分桶日期与 id 内嵌时间戳不一致」的 locator，断言 fail-closed 且不扫盘、不改桶。门槛：同一测试文件退出码 0。
- [ ] 4.3 确认 child thread 的 `threads/YYYY/MM/DD/{thread_id}`（`app/core/session_control_store.py`）同样按 UTC 且与 id 内嵌时间戳一致。门槛：`uv run pytest -q tests/unit/core/test_thread_creation.py` 退出码 0。

## 5. SQLite 主键与索引（D6）

- [ ] 5.1 断言 `nodes.node_id`、`thread_catalog.thread_id` 的表 DDL 未因 v7 变更（无新列、无新索引、无迁移 DDL）。门槛：`uv run pytest -q tests/unit/core/test_session_catalog_store.py tests/unit/core/test_session_control_store.py` 退出码 0。
- [ ] 5.2 补测试：v7 id 插入既有主键表后，按 id 排序≈按时间顺序。门槛：对应单测退出码 0。

## 6. 存量 UUIDv4 一次性显式迁移（D3）

- [ ] 6.1 审计并列出全部承载 canonical id 的持久载体（session/thread id、目录叶名、SQLite 主键、rollout/message_stream/trace/llm_request 内嵌引用、gateway 控制面记录）。门槛：审计清单落盘到 `out/tests/temp/uuidv7_openspec/artifacts/`，命令 `rg -l 'ses_[0-9a-f]{32}|thr_[0-9a-f]{32}' app` 退出码 0 且清单覆盖全部命中文件。
- [ ] 6.2 复用 `app/core/session_catalog_migration.py` 的 staging + journal + 隔离区形态，实现一次性、可恢复、带 source→target lineage 账本的 v4→v7 重编号迁移。门槛：`uv run pytest -q tests/unit/core/test_session_catalog_migration.py` 退出码 0。
- [ ] 6.3 补迁移中断恢复测试与「无法归属即 fail-closed/隔离、不扫盘吸收」测试。门槛：对应迁移测试退出码 0。
- [ ] 6.4 迁移完成后把校验器收紧为只接受 v7，并断言运行路径无 v4 双读、无旧 ID path alias。门槛：`rg -n 'v4|uuid4' app/core/session_catalog_store.py` 退出码 1；迁移收敛测试退出码 0。

## 7. JS 服务进程与浏览器前端边界（D7）

- [ ] 7.1 在 `src/workspace-services/{browser,terminal}/server/` 与 `src/clients/web/src/utils/media/mediaAttachments.ts` 的 id 生成点上方加中文注释，显式声明这些是非 canonical 身份、允许使用 v4，并说明原因（Node 无 `randomUUIDv7`；浏览器无 `Bun.*`）。门槛：`rg -n '非 canonical' src/workspace-services src/clients/web/src/utils/media/mediaAttachments.ts` 退出码 0。
- [ ] 7.2 补断言：canonical 校验器 MUST 拒绝这些非 canonical id 作为 session/thread 身份。门槛：对应单测退出码 0。
- [ ] 7.3 若前端 UI 改动，执行 `bun run --cwd src/clients/web build`。门槛：退出码 0。

## 8. 边界与引用（D8）

- [ ] 8.1 确认本 change 未定义/改写 VRN、scope、kind、拒绝码或 ResourceIdentity；引用处均为具名 change 名。门槛：`rg -n 'VRN|scope_id|拒绝码' openspec/changes/migrate-identifiers-to-uuidv7/specs` 仅出现引用语境。
- [ ] 8.2 把在途 change 的 v4 文本收口作为待 owner 处理项上报（`add-itemized-rollout-context` 的 `specs/itemized-rollout-context/spec.md`、`specs/rollout-checkpoint-storage/spec.md`；`add-context-injection-lifecycle` 的 `specs/context-injection-lifecycle/spec.md`、`tasks.md`）；本 change MUST NOT 代改。门槛：报告列出具名路径与冲突文本。

## 9. 质量门

- [ ] 9.1 `openspec validate migrate-identifiers-to-uuidv7 --strict` 输出 `Change 'migrate-identifiers-to-uuidv7' is valid`，退出码 0。
- [ ] 9.2 `openspec validate --strict --all` 退出码 0 且 `0 failed`（本 change 加入后为 40 passed）。
- [ ] 9.3 实施阶段的完整测试带进程外保护执行（按 AGENTS.md：`bun run test:matrix -- --suite=<id>` 或 `timeout <秒> bash -c 'ulimit -d 4194304; exec "$@"' bash <命令>`），退出码 0。

