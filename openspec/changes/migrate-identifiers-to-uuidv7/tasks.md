## 1. 显式依赖与生成来源（D1）

- [x] 1.1 在 `pyproject.toml` 的 `dependencies` 中显式新增 `uuid-utils>=0.16`，运行 `uv sync` 并确认 `uv.lock` 中 `uuid-utils` 仍是同一锁定版本 `0.16.0`。门槛：`uv sync` 退出码 0；`uv run python -c "import uuid_utils; print(uuid_utils.__version__)"` 退出码 0 且输出 `0.16.0`。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「1.1」。）**
- [x] 1.2 把 `app/core/identifier.py` 的 `create_uuid_hex()` 改为返回 `uuid_utils.uuid7().hex`；保留 `create_prefixed_id(prefix)` 的 `f"{prefix}_{hex}"` 外形。门槛：`uv run python -c "from app.core.identifier import create_uuid_hex; h=create_uuid_hex(); assert h[12]=='7' and h[16] in '89ab' and len(h)==32"` 退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「1.2」。）**
- [x] 1.3 增加 fail-closed 守卫：`uuid_utils` 导入失败或 `uuid7` 不可用时抛出详细错误，MUST NOT 回退 `uuid.uuid4()`。门槛：单测模拟导入缺失，断言抛错且无 v4 产出（`uv run pytest -q tests/unit/core/test_identifier.py` 退出码 0）。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「1.3」。）**

## 2. 单调性与时钟回拨（D2、D4c）

- [x] 2.1 为唯一工厂补充同毫秒单调测试：同进程内同一毫秒连续生成 20000 个 id，断言按 hex 排序与生成顺序逐字节一致且全部唯一。门槛：`uv run pytest -q tests/unit/core/test_identifier_uuidv7_monotonic.py` 退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「2.1」。）**
- [x] 2.2 补跨毫秒自然单调测试：连续生成 200000 个 id，断言全局有序且唯一。门槛：同一测试文件退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「2.2」。）**
- [x] 2.3 实现并测试时钟回拨钳制：注入早于上次生成时刻的时间源时，新 id MUST 不小于上一次。门槛：单测断言回拨场景下非递减，退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「2.3」。）**
- [x] 2.4 断言生成路径不传显式 `timestamp`（可静态检查或断言调用形态），并在代码注释中说明显式时间戳会破坏同毫秒单调。门槛：对应单测退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「2.4」。）**
- [x] 2.5（A3 量化）补一条量化上界测试：同一毫秒内生成 500000 个 id，断言同毫秒组最大规模被实测记录（约 3710）且组内全部有序唯一；并断言实现与文档只承诺「同进程内同毫秒非递减且唯一」+「跨进程共享 48 bit 毫秒分辨率」，不承诺跨进程同毫秒有序。门槛：对应测试退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「2.5」。）**

## 3. 校验层正名与单一 profile（D5）

- [x] 3.1 把 `app/core/session_catalog_store/`（原单文件已拆为同名包，见 `fbf56cdd`；校验器在 `validators.py`）的 `_validate_uuid_v4_payload` 正名为 `_validate_uuid_payload`，`_UUID_VERSION_HEX_INDEX` 语义改为要求 `version == 7`；删除任何 `v4` 命名残留；更新 `validate_session_id`/`validate_thread_id` 的 docstring 与注释为「UUIDv7 位 profile」。门槛：`uv run python -c "import app.core.session_catalog_store as s; print([n for n in dir(s) if 'uuid' in n.lower()])"` 退出码 0 且输出无 v4 命名。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「3.1」。）**
- [x] 3.2 同步 `app/protocol/canonical.py` 的注释（现写「payload 第 13 个 hex 位为 4（UUIDv4 version）…」）。门槛：`rg -n 'UUIDv4|非 v4|v4 bit' app/core/session_catalog_store app/protocol/canonical.py` 退出码 1（0 命中）。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「3.2」。）**
- [x] 3.3 补负向测试：payload 第 13 个 hex 为 `4` 的 id MUST 被拒绝。门槛：`uv run pytest -q tests/unit/core/test_canonical_identifier_matrix.py` 退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「3.3」。）**
- [x] 3.4 更新 `tests/unit/core/test_canonical_identifier_matrix.py` 的 docstring（现写「非 v4 bits」）与 `make_session_id`/`make_thread_id`（现用 `uuid.uuid4().hex`）为 v7 生成。门槛：`rg -n 'uuid4' tests/unit/core/test_canonical_identifier_matrix.py` 退出码 1。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「3.4」。）**

## 4. 日期桶与 id 内嵌时间戳一致性（D4）

- [x] 4.1 在 `validate_storage_relative_locator()` 中加入「`sessions/YYYY/MM/DD` 的 UTC 日期 == id 内嵌 48 bit 毫秒时间戳的 UTC 日期」断言；不一致抛显式完整性错误。门槛：`uv run pytest -q tests/unit/core/test_session_catalog_store.py` 退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「4.1」。）**
- [x] 4.2 补负向测试：构造「分桶日期与 id 内嵌时间戳不一致」的 locator，断言 fail-closed 且不扫盘、不改桶。门槛：同一测试文件退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「4.2」。）**
- [x] 4.3 确认 child thread 的 `threads/YYYY/MM/DD/{thread_id}`（`app/core/session_control_store.py`）同样按 UTC 且与 id 内嵌时间戳一致。门槛：`uv run pytest -q tests/unit/core/test_thread_creation.py` 退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「4.3」。）**
- [x] 4.7（规范层可机械复核项，**第二轮 2026-10-01 定稿**）日期桶与 id 内嵌时间的**同一时间源**：系统 MUST 以唯一时间源 `app/core/identifier.py::effective_now_ms()`（其 datetime 投影 `effective_now()`）= `max(墙钟毫秒, 上次已发放毫秒)` 同时推出 id 内嵌 48 bit 毫秒时间戳与 `sessions/YYYY/MM/DD` 分桶 UTC 日期，MUST NOT 从两处独立取时。**实测（已接线，非滞留项）**：`app/core/session_creation.py:319` 与 `app/core/thread_creation.py:695` 均 `created_at=effective_now()`；`app/core/session_catalog_store/creation_journal.py:164-173` 由**同一** `created_at` 推出 `create_ms = to_epoch_ms(created_at)` → `create_prefixed_id_at("ses"/"thr", create_ms)` 派生 id，并用 `created_at.astimezone(UTC).date()` 冻结 `sessions/YYYY/MM/DD` 分桶——id 内嵌时间与分桶**同源同一已钳制值**。门槛（可机械复核）：4.1 的「分桶日期 == id 内嵌时间戳 UTC 日期」断言由该同源关系直接推出、MUST NOT 依赖两次独立取时的偶然一致；回拨场景由 `tests/unit/core/test_session_creation.py::test_create_clamps_clock_rollback_keeping_bucket_consistent` 机械佐证（分桶被钳到 2026-06-01 且与 id/main_thread 内嵌时间同日），**467 passed**（见 §台账补勾证据「4.7」）。
- [x] 4.4（A3）实现「回拨与分桶互不冲突」语义：创建流程用同一已钳制时间源 `effective_created_ms = max(monotonic_now_ms, last_issued_ms)` 同时推出 id 内嵌时间戳与分桶 UTC 日期。门槛：新增测试断言「注入回拨后创建 session，其分桶日期与 id 内嵌时间戳一致且不报错」，退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「4.4」。）**
- [x] 4.5（A3）断言默认语义下回拨 MUST NOT 变成用户可见故障（不拒绝创建）；若实现选择「拒绝并报告」的显式配置语义，MUST 有对应测试并在文档判死二选一。门槛：`uv run pytest -q <该测试>` 退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「4.5」。）**
- [x] 4.6（A3）量化断言的负向测试：断言文档/实现不声称跨进程同毫秒有序或主键严格按时间相邻。门槛：对应断言测试退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「4.6」。）**

## 5. SQLite 主键与索引（D6）

- [x] 5.1 断言 `nodes.node_id`、`thread_catalog.thread_id` 的表 DDL 未因 v7 变更（无新列、无新索引、无迁移 DDL）。门槛：`uv run pytest -q tests/unit/core/test_session_catalog_store.py tests/unit/core/test_session_control_store.py` 退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「5.1」。）**
- [x] 5.2 补测试：v7 id 插入既有主键表后，按 id 排序≈按时间顺序。门槛：对应单测退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「5.2」。）**

## 5A. 阻断性前置：哈希与幂等键审计（D9，MUST 在 §6 之前完成）

- [x] 5A.1 审计全仓 `content_hash` / `contribution_content_hash` / `plan_hash`(`context_plan_hash`) / `request_hash`(`context_request_hash`) / itemized plan-hash / 幂等键 / 去重键的输入，逐条判定是否**直接或间接**包含 `session_id` / `thread_id` / 资源 id（含 `create_prefixed_id` 产物、`display_uri`、`entry_identity`、catalog payload）。门槛：审计报告落盘到 `out/tests/temp/uuidv7_openspec/artifacts/`，命令 `rg -rn 'sha256_jcs|hashlib.sha256|idempotency_key' app --glob '*.py' -l` 退出码 0 且报告覆盖全部命中文件。
- [x] 5A.2 命中清单必须含「文件 + 符号 + 判定依据」；已实测命中至少包括 `app/domain/itemized/hash/plan_hash.py` 的 `context_plan_hash`（含 `session_id`/`plan_id`/`ref_id`）、`app/domain/itemized/hash/request_hash.py` 的 `context_request_hash`、`app/core/thread_creation.py` 的 `compute_thread_creation_preimage_hash`（含 `session_id`/`thread_id`）、以及 `app/services/infrastructure/rollout_context/storage/transaction.py` 的 `default_idempotency_key(commit_kind, subject_id, outcome, metadata)`（**签名显式含 `subject_id`，生产调用点即 canonical id**）。门槛：`rg -n 'session_id|thread_id|subject_id' app/domain/itemized/hash/plan_hash.py app/core/thread_creation.py app/services/infrastructure/rollout_context/storage/transaction.py` 退出码 0，且报告逐条记录。
- [x] 5A.2b A5 强制处置：`default_idempotency_key` 已判定**会漂移**，MUST 进 §5A 的处置表并明确「随迁移一致重算」或「该 id 不参与迁移」，MUST NOT 只登记不处置。门槛：报告处置表中存在该符号条目且处置非空。
- [x] 5A.3 必须附「已验证**不**含 id」的**负向证据**，例如 `app/domain/itemized/hashing.py` 的 `content_hash` 输入仅 `{payload_kind, payload}`、`contribution_content_hash` 仅 `{contribution_kind, body}`，`app/core/session_creation.py` 的 `compute_session_creation_preimage_hash` 四元组 `{workspace_id, parent_node_id, title, session_metadata}` 不含 `session_id`。门槛：`rg -n 'def content_hash|def contribution_content_hash' app/domain/itemized/hashing.py` 退出码 0 且报告记录输入字段清单。
- [x] 5A.4 逐条给出处置：以 id 为输入的哈希/幂等键 MUST 明确为「随迁移一致重算」或「该 id 不参与迁移」，并配验证；MUST NOT 留成「迁移后哈希漂移但无人负责」。门槛：报告逐条标注处置；未落定条数 MUST 为 0。
- [x] 5A.5 审计未落定前，§6 的迁移任务 MUST NOT 执行任何重编号。门槛：执行记录证明 §6 在 §5A 全部勾选后才开工。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「5A.5」。）**
> **（2026-09-30 补齐，5A.1–5A.4 已满足；5A.5 留作 §6 开工时的执行记录）**：交付物已补齐到 `out/tests/temp/uuidv7_openspec/artifacts/hash_audit_*`（源报告 `out/tests/temp/uuidv7_hash_audit/artifacts/`）。门槛命令 `rg -rn 'sha256_jcs|hashlib.sha256|idempotency_key' app --glob '*.py' -l` 实测 **191 个文件全覆盖、未归类 = 0**（三 token 各自命中 56/81/102，并集 191）；互斥归类为 A 原语本体 3、B 内容哈希 39、C canonical 身份原型 2、D opaque 56、E 非 canonical id 派生 23、F 含 canonical id 的 preimage 68。**F 类 68 个文件逐符号处置、未落定 = 0**（随迁移一致重算 66、fork 链路 key 重映射重算 1、该 id 不参与迁移 1）。负向证据齐：`content_hash` 仅 `{payload_kind, payload}`、`contribution_content_hash` 仅 `{contribution_kind, body}`、`compute_session_creation_preimage_hash` 仅 `{workspace_id, parent_node_id, title, session_metadata}`，另附 17 条「只吃正文/默认值」负向。
>
> **§5A 已落定的两条边界，供 §6 直接复用**：① `default_idempotency_key(commit_kind, subject_id, outcome, metadata)` 的 `subject_id` 即 canonical id，判定**会漂移**，处置 = 随迁移一致重算（实测不同 subject_id 得到不同 key）；② `compute_thread_creation_preimage_hash` 含 session_id/thread_id/delegation_id，处置 = 随迁移一致重算（实测 v4-like 与 v7-like 摘要不同）。唯一「该 id 不参与迁移」是 `core/session_subtree_delete.py` 的 `idempotency_key`（瞬时隔离目录名，随删除回收，非持久身份）。
>
> **迁移面口径（owner 裁定）**：本审计按「是否含 canonical id（**含其派生面**）」判定，把由 message_id/turn_id/execution_id 派生、或经 `default_idempotency_key(subject_id=...)` 携带 canonical id 的哈希/幂等键一并纳入「重算」。**§6 不得收窄为只重编号 `ses_`/`thr_` 字面**，否则本报告的间接派生项会变成「迁移后漂移但无人负责」。本项门槛满足。

## 5B. 阻断性前置：gateway 控制面库逐表分类（D10，MUST 在 §6 之前完成）

- [x] 5B.1 对控制面库（`app/gateway/control/gateway_state/` 包，原单文件已随 `579001a8` 拆分 等）承载 session 身份的表逐表分类为 `migrate` / `explicitly_invalidated` / `not_affected`。**已实测控制面只有两个 SQLite 库**：库 A `gateway.sqlite`（`app/gateway/main.py` 实例化，建表权威在 `gateway_state.py` 的 `_GATEWAY_MIGRATIONS`，21 张表 + 框架表 `schema_migrations`）、库 B `federation/control.sqlite`（`app/gateway/federation/store.py`，4 张表）。门槛：报告给出逐表分类与判定依据，且 `rg -n 'session_id|access_session_id' app/gateway/control/gateway_state/` 退出码 0。
- [x] 5B.2 `user_access_lease` MUST NOT 归入 `explicitly_invalidated`，除非实测证明其为「可安全丢弃的租约」；否则归 `migrate`。门槛：报告给出该表的分类与安全依据。
- [x] 5B.3 归入 `explicitly_invalidated` 的表 MUST 有用户可见的显式报告（表名、行数、失效原因），MUST NOT 静默重建。门槛：对应显式报告路径与测试存在，退出码 0。
> **（2026-09-30 owner 裁定，落定分类；取证见 `out/tests/temp/uuidv7_control_db_audit/artifacts/report.md`）**：
>
> - 库 A：`user_view_state` = **migrate**（`session_id` 是 canonical `ses_`，Gateway 不校验直接透传，承载用户阅读位置；静默失效即默默失败）；`user_access_lease`、`guest_tracking` 及其余 18 张 = **not_affected**（id 列为 `access-<token_urlsafe>` / `guest-<token_urlsafe>` / `new_config_id()` / `uuid4` / `runtime_lease_<uuid4>`，均非工厂前缀；已对含 JSON 的列做全列工厂形态扫描，0 命中）。
> - 库 B：`federation_route_hint` = **migrate**（同时承载 canonical `session_id` 与 `resolved_main_thread_id`）；其余 3 张 = **not_affected**。
> - **未分类表数 = 0；归 `explicitly_invalidated` 的表数 = 0**，故 5B.3 在空集上满足。**若实施期改判任一表为 `explicitly_invalidated`，5B.3 立即不满足**：实测 `rg -rn 'explicitly_invalidated' app tests scripts tools src` 退出码 1（0 命中），该报告机制与测试**尚不存在、需新实现**。
> - 5B.2 依据：`user_access_lease.access_session_id` 由 `user_access.py` 的 `_new_session_id()` 生成，非 canonical；TTL 45s + `heartbeat` 续期 + `acquire_user(takeover=False)` 抛 `UserLeaseOccupiedError` + 写入侧 `_assert_active_lease` 做四元组守卫，且行落 SQLite 断电重启后仍在用——**不是可安全丢弃的租约**，删除会造成双访问。
> - **控制面库之外另有 4 处含 canonical id 的持久面已转登 §6.1b**（会话索引缓存 JSON、profile 的 `collapsed_session_ids`、generators 的 `placement.session_id`/`message_id`/`job_id`、工作区后端持久面）。
> - `runtime_lease_<uuid4>` / `target_generation_<uuid4>` 属非工厂前缀，已明文纳入 §7 豁免。

## 6. 存量 UUIDv4 一次性显式迁移（D3）

### 6.0 owner 裁定（2026-10-01 定稿）：无运行时开关 + 存量 v4 一次性显式处置

> **状态：已定稿（不再是「待裁定」）。** 本裁定由 owner 直接给出，**不与第二轮 §6.0 的 A/B/C 三选项机械对应**（方向接近「C 其它形态」）；凡与第二轮选项文字冲突处，**以本条裁定为准**。
>
> **§6.2/§6.3/§6.5/§6.6 保持未勾**，原因是「一次性显式处置路径尚未实现/未测」，**不再是因为「迁移形态待裁定」**；MUST NOT 为凑勾选而勾选。

**裁定结论**：

1. **不接受运行时维护开关**：`identity_profile_migration_active`（以及任何等价「窗口期接受 `v4|v7`」的运行时分叉）**判死**。理由：AGENTS.md 第 1 条严禁双轨，**运行时开关就是双轨**；且 HEAD 已机械断言源码中不存在该开关（见下「事实 3」）。**分支 B（维护窗口 + 双接受）就此判死，不再是候选。**
2. **坚持 HEAD 现状（canonical 校验器只接受 v7）**，这是 O-1 的既有裁定，**不推翻**。
3. **存量 v4 必须一次性显式处置**，不得静默吸收、不得扫盘重建。判据是既有迁移机对非法 id 的 `illegal_id` 隔离（`app/core/session_catalog_migration` 的 `_preflight`/`_physical` 段）。要求：启动/迁移时遇到 v4 canonical id MUST **fail-closed** 并给出**可操作的显式处置指引（隔离 + 报告）**，MUST NOT 回退 v4、MUST NOT 双读。
4. **勾选口径变更**：§6.2/§6.3/§6.5/§6.6 的验收门从「运行时开关可用」改为「**一次性显式处置路径可用且有测试**」；因该路径尚未实现/未测，**逐条保持未勾**。

**判死的方案（原分支 B）**：维护窗口 + `identity_profile_migration_active` + 窗口期接受 `v4|v7`。— **判死**：窗口期的运行时双接受即双轨，与 AGENTS.md 第 1 条及本 change「彻底根除双轨」目标冲突。

**仍有效的事实（第二轮 2026-10-01 实测）**：

1. **生产读写路径的校验器只接受 v7，不接受 v4。** `app/core/session_catalog_store/validators.py:31-55` 的 `validate_session_id`/`validate_thread_id` → `_validate_uuid_payload` 对 payload 第 13 个 hex 位**硬要求 `== "7"`**，非 v7 一律 `raise ValueError`。
   原始输出：`rg -n 'v4|uuid4' app/core/session_catalog_store` → **EXIT=1（0 命中）**。
2. **迁移机对非法 id 直接隔离，绝不重编号——这就是「一次性显式处置」的既有判据。** `app/core/session_catalog_migration` 的 `_preflight`/`_physical` 段中，`validate_session_id(node.node_id)` 失败即返回 `illegal_id`，节点进隔离区而非被读出重写；**不存在「放宽读到 v4 再重编号」的路径**。
3. **HEAD 已机械断言源码中不存在维护开关。** `tests/unit/core/test_canonical_identifier_matrix.py:170` 的 `test_identifier_profile_converged_to_v7_only`（注释写明「O-1 分支 A：不引入维护开关」）断言 `"identity_profile_migration_active" not in source` 且 `"_validate_uuid_v4_payload" not in source`。
   原始输出：`rg -n 'identity_profile_migration_active' app tests scripts` → 仅 1 行命中，即该断言行本身（`tests/unit/core/test_canonical_identifier_matrix.py:173`），**app/ 生产源码 0 命中**。

**存量 v4 一次性显式处置的验收口径（§6.2/§6.3/§6.5/§6.6 共用，可判否）**：

- **MUST**：启动/迁移遇到 v4 canonical id → **fail-closed**（`illegal_id` 隔离），MUST NOT 回退 v4、MUST NOT 双读、MUST NOT 扫盘重建。
- **MUST**：给出**可操作的显式处置指引**——隔离报告须含**被隔离 id、物理路径、原因、建议动作**，MUST NOT 静默丢弃。
- **MUST**：有测试机械证明「v4 存量被隔离且报告可见、不静默吸收」。
- **MUST NOT**：引入 `identity_profile_migration_active` 或任何运行时开关、放宽校验器到 `v4|v7`。

**定稿后果（对原始动机：`sessions/YYYY/MM/DD/` 分桶与 SQLite 主键）**：

| 对象 | 定稿后果 |
|---|---|
| **存量 v4 分桶** | **保持现状**：存量继续停留在原 v4 所在 UTC 日期桶（由另存 `created_at` 决定），新数据落 v7 日期桶；两段历史共存，**存量不享受「id 自校验分桶」收益**。 |
| **存量 v4 主键** | **保持 v4 文本序**：`nodes.node_id`/`thread_catalog.thread_id` 等存量主键不改写，B-tree 时间局部性**只对新写入的 v7 段生效**；同一表内 v4/v7 混合，排序≈时间仅对新增段成立。 |
| **存量 v4 的可达性** | 遇到即 fail-closed 隔离 + 显式报告，**不静默吸收、不扫盘重建**；由用户/owner 按报告中的处置指引显式决定归档或失效。 |

**这是接受「运行时无双轨」的代价，已定稿，不再重新讨论。**

- [x] 6.1 按 design D12 的「工厂前缀 × 持久面」全集矩阵（`IdentifierPrefix` 的 33 个前缀）枚举迁移面，MUST NOT 只匹配 `ses_`/`thr_` 字面。门槛：矩阵落盘到 `out/tests/temp/uuidv7_openspec/artifacts/`；命令 `rg -n 'IdentifierPrefix = Literal' -A40 app/core/identifier.py` 退出码 0 且矩阵行数 MUST 等于该 `Literal` 的前缀数（33）；每个持久面前缀 MUST 给出具名载体证据，每个非持久面前缀 MUST 给出「不落盘」的负向证据。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「6.1」。）** **（独立复核 2026-10-01 实测：`typing.get_args(IdentifierPrefix)` = 33 且去重后仍 33，等于矩阵行数与 `Literal` 前缀数；见 §台账「独立复核证据（2026-10-01）」。）**
- [x] 6.1b 复核已实测的持久面漏项至少覆盖 `op_`（`navigation_mutation_records` 主键）、`strm_`（`message_streams/*.jsonl` 文件名）、`msg_`（rollout `messages.message_id`）、`evt_`/`snapshot_`（message_stream JSONL）、`part_`（`item_parts.part_id`）、`goal_`（`goal.json`）、`gen_`/`grun_`（generators 文件）、`team_`/`ttask_`/`tevt_`（team JSON/JSONL），**外加 2026-09-30 控制面审计新点的四处非 SQLite 持久面**：控制面会话索引缓存 `state/gateway/indexes/session-catalogs/*.json`（含真实 `ses_`/`thr_`）、用户档案 `state/gateway/users/<id>/profile.jsonc` 的 `session_sidebar.collapsed_session_ids`（含 canonical `ses_`）、Gateway generators 产出的 `state/gateway/generators/*.json` 与 `generation-runs/**`（含 `placement.session_id`/`message_id`/`job_id`）、工作区后端持久面（`workspace_activity.session_id`、`attachment_*.owner_session_id`）。门槛：报告逐项列出具名路径与调用点。**注意：迁移 generators 的产出数据文件不等于修改 `app/gateway/control/generators.py` 源码，后者为受保护路径，全程禁改。** **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「6.1b」。）**
- [ ] 6.2（**存量 v4 一次性显式处置；验收门已按 §6.0 裁定改为「处置路径可用且有测试」**）在既有 `app/core/session_catalog_migration` 的 `illegal_id` 隔离机制上，落成**可操作的显式处置路径**：启动/迁移遇到 v4 canonical id MUST fail-closed 隔离并产出显式报告（被隔离 id、物理路径、原因、建议动作），MUST NOT 回退 v4、MUST NOT 双读、MUST NOT 扫盘重建、MUST NOT 引入运行时维护开关。门槛：处置路径有测试且 `uv run pytest -q tests/unit/core/test_session_catalog_migration.py` 退出码 0。**当前未实现/未测，保持未勾。** **独立复核 2026-10-01 实测：fail-closed 隔离本体已在位（`_preflight.py:370-373` 经 `validate_session_id` 收 v7 → `illegal_id`；`_physical.py:310-347` 隔离到 `orphaned/session-catalog-migration/`；`scripts/migrate_session_catalog.py:152-155` 打印被隔离 id 与原因；`tests/unit/core/test_session_catalog_migration.py` 88 passed），但裁定要求的「物理路径 + 建议动作」报告字段尚未提供（`_contracts.py:15-20` 的 `QuarantinedNode` 只有 `node_id`/`reason`；全仓 `rg '建议动作|suggested_action' app scripts` 0 命中），故本条仍保持未勾（缺口 = 报告字段 + 断言，不再含迁移机制本体）。**
- [ ] 6.3（**存量 v4 一次性显式处置；随 §6.2**）补测试：迁移遇到 v4 存量时 MUST fail-closed 隔离且报告可见（含被隔离 id/路径/原因/建议动作），MUST NOT 静默吸收、MUST NOT 扫盘重建；并覆盖「处置中断可恢复」。门槛：对应迁移测试退出码 0。**当前未测，保持未勾。**
- [x] 6.4（**主门槛已满足；「存量 v4 显式处置测试」子门槛随 §6.2 保持未达，见 §台账补勾证据「6.4」**）校验器收紧为只接受 v7，并断言运行路径无 v4 双读、无旧 ID path alias。门槛：`rg -n 'v4|uuid4' app/core/session_catalog_store` 退出码 1（已满足，实测 0 命中）；存量 v4 显式处置测试退出码 0（**已实测 `tests/unit/core/test_session_catalog_migration.py` 88 passed（EXIT=0）；但「报告含被隔离 id/路径/原因/建议动作」仍缺路径与建议动作字段（见 §6.2 独立复核），故子门槛按验收口径仍未完全达，主门槛已满足。**）。 **（独立复核 2026-10-01 实测：`rg -n 'v4|uuid4' app/core/session_catalog_store` EXIT=1（0 命中）；`test_identifier_profile_converged_to_v7_only` 绿；见 §台账「独立复核证据（2026-10-01）」。）**
- [ ] 6.5（**已按 §6.0 裁定改写：不再是运行时维护开关**）存量 v4 一次性显式处置的**负向断言**：系统 MUST NOT 引入运行时维护开关 `identity_profile_migration_active`（判死），MUST NOT 放宽校验器到 `v4|v7`；遇到 v4 canonical id MUST fail-closed 隔离并给可操作处置指引。门槛：单测断言「`rg -n 'identity_profile_migration_active' app` 0 命中」且「v4 存量被隔离 + 报告含 id/路径/原因/建议动作」，`uv run pytest -q <该测试>` 退出码 0。**当前未实现/未测，保持未勾。**
- [ ] 6.6（**已按 §6.0 裁定改写**）存量 v4 显式处置的**「不静默」断言**：处置 MUST 产生用户可见报告（列出被隔离 id 与路径），MUST NOT 静默丢弃、MUST NOT 扫盘重建、MUST NOT 新旧代码并行双读。门槛：对应测试退出码 0。**当前未测，保持未勾。**
- [x] 6.7（A2，已按 §6.0 裁定改写措辞）**无运行时开关的收敛断言**：必须有一条测试证明「`v4` 位 profile 的 canonical 身份被拒绝且 `v7` 被接受」，以机械证明**不存在窗口期、不存在运行时双轨**。门槛：该测试退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「6.7」。）**

## 7. JS 服务进程与浏览器前端边界（D7）

- [x] 7.1 在 `src/workspace-services/{browser,terminal}/server/` 与 `src/clients/web/src/utils/media/mediaAttachments.ts` 的 id 生成点上方加中文注释，显式声明这些是非 canonical 身份、允许使用 v4，并说明原因（Node 无 `randomUUIDv7`；浏览器无 `Bun.*`）。门槛：`rg -n '非 canonical' src/workspace-services src/clients/web/src/utils/media/mediaAttachments.ts` 退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「7.1」。）**
- [x] 7.1c 把运行时非工厂前缀豁免纳入正名后的枚举：`runtime_lease_<uuid4>`（`app/gateway/control/gateway_state/` 包）与 `target_generation_<uuid4>`（Gateway 目标生成标识）经 2026-09-30 实测确认**不是 `IdentifierPrefix` 工厂前缀**，与 §7.1b 的豁免集同属「非 canonical 身份」，MUST 在豁免枚举中显式登记，MUST NOT 被当作 v4 残留误列入迁移面。门槛：报告给出具名载体与生成点，且豁免枚举测试覆盖这两者。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「7.1c」。）**
- [x] 7.2 补断言：canonical 校验器 MUST 拒绝这些非 canonical id 作为 session/thread 身份。门槛：对应单测退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「7.2」。）**
- [x] 7.3 若前端 UI 改动，执行 `bun run --cwd src/clients/web build`。门槛：退出码 0。 **（第二轮 2026-10-01 复跑勾选：见 §台账补勾证据「7.3」。）**

## 8. 边界与引用（D8）

- [x] 8.1 确认本 change 未定义/改写 VRN、scope、kind、拒绝码或 ResourceIdentity；引用处均为具名 change 名。门槛：`rg -n 'VRN|scope_id|拒绝码' openspec/changes/migrate-identifiers-to-uuidv7/specs` 仅出现引用语境。 **（本次实测（提交 `f6fc990f`）现状：`rg -n 'VRN|scope_id|拒绝码' openspec/changes/migrate-identifiers-to-uuidv7/specs` 退出码 0，仅 3 处命中，全部为「本 capability 不定义 VRN/scope_id/拒绝码，只具名引用」的引用语境（`spec.md:3`、`:121` 的 requirement 标题、`:123`）；门槛满足。）**
- [x] 8.2 在 design D11 点名四处「仍含 v4 表述、需由各自 owner 收口」的位置（文件 + capability + requirement/任务）：`add-itemized-rollout-context` 的 `specs/itemized-rollout-context/spec.md`（capability `itemized-rollout-context`，requirement「产品 Session、durable Thread 与 LangGraph namespace 必须严格分层」）与 `specs/rollout-checkpoint-storage/spec.md`（capability `rollout-checkpoint-storage`，requirement「rollout storage 必须以 SessionThread 为物理与事务 owner」）；`add-context-injection-lifecycle` 的 `specs/context-injection-lifecycle/spec.md`（capability `context-injection-lifecycle`，requirements「Context lifecycle owner 必须精确为 SessionThread」「生命周期场景必须进入统一 Web E2E 验收模块」）与 `tasks.md`（任务 2.1）；声明本 change 是 id 生成位 profile 唯一 owner、上述文本兑现时 MUST 引用本 change、MUST NOT 复述取值；本 change MUST NOT 代改。门槛：报告列出具名路径与冲突文本。 **（本次实测（提交 `f6fc990f`）现状：D11 已交付四处具名路径（`design.md:160-171`：itemized 的 `specs/itemized-rollout-context/spec.md`、`specs/rollout-checkpoint-storage/spec.md`；CIL 的 `specs/context-injection-lifecycle/spec.md`、`tasks.md` 任务 2.1）；复跑 `rg -n '第 13 个 hex|UUIDv4|v4 bit profile'` 对上述四处文件零命中，四处 v4 表述均已由各自 owner 收口引用本 change；门槛「报告列出具名路径与冲突文本」满足，本 change 未代改。）**
- [x] 8.3 核验四方 owner 已自行在其产物中收口并引用本 change（本 change MUST NOT 代改这四处）。门槛：`rg -n 'migrate-identifiers-to-uuidv7' openspec/changes/add-itemized-rollout-context openspec/changes/add-context-injection-lifecycle` 退出码 0（由 owner 侧提交达成，本 change 只核验）。 **（本次实测（提交 `f6fc990f`）现状：`rg -n 'migrate-identifiers-to-uuidv7' openspec/changes/add-itemized-rollout-context openspec/changes/add-context-injection-lifecycle` 退出码 0，命中 14 行（itemized: `tasks.md:100/143`、`design.md:745/1000`、`proposal.md:34`、`specs/itemized-rollout-context/spec.md:15/76`、`specs/rollout-checkpoint-storage/spec.md:231`、`specs/session-turn-history/spec.md:139`；CIL: `tasks.md:13/116`、`design.md:636`、`specs/context-injection-lifecycle/spec.md:721/1038`）；门槛满足。）**
- [x] 8.4 在四方收口完成后重跑 `openspec validate --strict --all`。门槛：输出 `0 failed` 且退出码 0。 **（本次实测（提交 `f6fc990f`）现状：`openspec validate --strict --all` 输出 `Totals: 40 passed, 0 failed (40 items)`，退出码 0；门槛满足。）**

## 9. 质量门

- [x] 9.1 `openspec validate migrate-identifiers-to-uuidv7 --strict` 输出 `Change 'migrate-identifiers-to-uuidv7' is valid`，退出码 0。 **（本次实测（提交 `f6fc990f`）现状：`openspec validate migrate-identifiers-to-uuidv7 --strict` 输出 `Change 'migrate-identifiers-to-uuidv7' is valid`，退出码 0；门槛满足。）**
- [x] 9.2 `openspec validate --strict --all` 退出码 0 且 `0 failed`（本 change 加入后为 40 passed）。 **（本次实测（提交 `f6fc990f`）现状：`openspec validate --strict --all` 输出 `Totals: 40 passed, 0 failed (40 items)`，退出码 0；门槛满足。）**
- [ ] 9.3 实施阶段的完整测试带进程外保护执行（按 AGENTS.md：`bun run test:matrix -- --suite=<id>` 或 `timeout <秒> bash -c 'ulimit -d 4194304; exec "$@"' bash <命令>`），退出码 0。

## 台账补勾证据（第二轮 2026-10-01）

> 本节为 §1–§5、§6.1/§6.1b/§6.4/§6.7、§7.1/§7.1c–§7.3 的**可复现证据引用**。所有门槛命令均在仓库根执行；原始输出落盘在 `out/tests/temp/uuidv7_legacy_remap/artifacts/`。第三方可逐条重跑复核。

**独立复核证据（2026-10-01，`/root/uuidv7_final_review`）**：见本节末「独立复核证据（2026-10-01）」小节与 `out/tests/temp/uuidv7_final_review/artifacts/report.md`。

**原始输出文件**：`round2_gates.txt`（依赖/生成/正名/导入门禁）、`round2_suite_identifier.txt`（identifier 族 86 passed）、`round2_suite_creation.txt`（catalog/creation 族 467 passed）、`round2_web_build.txt`（前端 build EXIT=0）、`round2_openspec_change.txt` / `round2_openspec_all.txt`（openspec 校验）。

| 编号 | 门槛命令（仓库根） | 实测结果 |
|---|---|---|
| 1.1 | `uv lock --check`；`uv run --no-sync python -c "import uuid_utils; print(uuid_utils.__version__)"`；`grep -n 'uuid-utils' pyproject.toml` | 0.16.0；pyproject.toml:39 `"uuid-utils>=0.16"`；EXIT=0（round2_gates.txt） |
| 1.2 | `uv run --no-sync python -c "from app.core.identifier import create_uuid_hex; h=create_uuid_hex(); assert h[12]=='7' and h[16] in '89ab' and len(h)==32"` | EXIT=0（round2_gates.txt） |
| 1.3 | `uv run --no-sync pytest -q tests/unit/core/test_identifier.py` | test_uuid_utils_missing_fails_closed / test_uuid7_unavailable_fails_closed 均绿；86 passed（round2_suite_identifier.txt） |
| 2.1 | `... pytest -q tests/unit/core/test_identifier_uuidv7_monotonic.py` | test_same_millisecond_batch_is_non_decreasing_and_unique / test_prefixed_ids_same_millisecond_are_non_decreasing；86 passed |
| 2.2 | 同上 | test_cross_millisecond_batch_is_globally_ordered_and_unique；86 passed |
| 2.3 | 同上 | test_effective_now_ms_clamps_clock_rollback / test_clock_rollback_does_not_produce_smaller_id；86 passed |
| 2.4 | 同上 | test_generation_path_does_not_pass_explicit_timestamp / test_generation_docstring_forbids_explicit_timestamp；86 passed |
| 2.5 | 同上 | test_quantified_same_millisecond_density_upper_bound / test_documented_contract_only_promises_same_process_same_ms_order；86 passed |
| 3.1 | `uv run --no-sync python -c "import app.core.session_catalog_store as s; print([n for n in dir(s) if 'uuid' in n.lower()])"` | `['uuid7_embedded_utc_date']`（无 v4 命名）；EXIT=0 |
| 3.2 | `rg -n 'UUIDv4|非 v4|v4 bit' app/core/session_catalog_store app/protocol/canonical.py` | EXIT=1（0 命中） |
| 3.3 | `... pytest -q tests/unit/core/test_canonical_identifier_matrix.py` | test_v4_bit_profile_session_id_rejected；86 passed |
| 3.4 | `rg -n 'uuid4' tests/unit/core/test_canonical_identifier_matrix.py` | EXIT=1（0 命中） |
| 4.1 | `... pytest -q tests/unit/core/test_session_catalog_store.py` | validators.py:88 断言 + test_validate_storage_relative_locator_rejects_bucket_id_drift；467 passed（round2_suite_creation.txt） |
| 4.2 | 同上 + `test_canonical_identifier_matrix.py` | test_validate_storage_relative_locator_rejects_bucket_id_drift / test_storage_locator_date_drift_from_embedded_time_rejected；绿 |
| 4.3 | `... tests/unit/core/test_session_control_store.py` 等 | test_thread_locator_date_drift_from_embedded_time_rejected；467 passed |
| 4.7 | `rg -n 'effective_now' app --glob '*.py'` + `... tests/unit/core/test_session_creation.py` | session_creation.py:319 / thread_creation.py:695 = effective_now()；creation_journal.py:164-173 同一 created_at 派生 id 与分桶；test_create_clamps_clock_rollback_keeping_bucket_consistent；467 passed |
| 4.4 / 4.5 | 同上 | test_create_clamps_clock_rollback_keeping_bucket_consistent（record_state=="published"，分桶钳到 2026-06-01）；467 passed |
| 4.6 | `... test_identifier_uuidv7_monotonic.py` | test_documented_contract_only_promises_same_process_same_ms_order；86 passed |
| 5.1 | `... tests/unit/core/test_session_catalog_store.py tests/unit/core/test_session_control_store.py` | test_nodes_ddl_frozen_for_uuidv7 / test_nodes_primary_key_columns_unchanged / test_thread_catalog_primary_key_frozen_for_uuidv7；467 passed |
| 5.2 | 同上 | test_uuidv7_primary_key_ordering_matches_time_order / test_thread_catalog_uuidv7_ordering_matches_time_order；467 passed |
| 5A.5 | `rg -n 'identity_profile_migration_active' app` | §5A.1–5A.4 已勾（08f9caae）；§6 从未开工（app/ 0 命中）；执行记录成立 |
| 6.1 | `rg -n 'IdentifierPrefix = Literal' -A40 app/core/identifier.py` + `typing.get_args` | EXIT=0；前缀数 = 33，与矩阵行数一致；矩阵落盘 uuidv7_openspec/artifacts/prefix_persistence_matrix_6_1.md |
| 6.1b | 见 report.md §三·3.3 具名载体表 | 逐项具名路径与调用点齐 |
| 6.4 | `rg -n 'v4|uuid4' app/core/session_catalog_store`；`... test_canonical_identifier_matrix.py` | EXIT=1；test_identifier_profile_converged_to_v7_only；86 passed。**子门槛「迁移收敛测试」依赖 §6.2，保持未达** |
| 6.7 | 同上 | test_identifier_profile_converged_to_v7_only（v4 拒 / v7 收）；86 passed |
| 7.1 | `rg -n '非 canonical' src/workspace-services src/clients/web/src/utils/media/mediaAttachments.ts` | EXIT=0（7 处命中） |
| 7.1c | `uv run --no-sync pytest -q tests/unit/core/test_non_canonical_exemptions.py` | tests/unit/core/test_non_canonical_exemptions.py（本轮提交 a08647a4）；86 passed |
| 7.2 | 同上 | test_canonical_validator_rejects_non_canonical_id / test_exemption_must_not_expand_to_canonical_ids；86 passed |
| 7.3 | `bun run --cwd src/clients/web build` | EXIT=0（built in 17.00s，round2_web_build.txt） |

**复跑口径**：`86 passed` = `timeout 1200 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run --no-sync pytest -q tests/unit/core/test_identifier.py tests/unit/core/test_identifier_uuidv7_monotonic.py tests/unit/core/test_canonical_identifier_matrix.py tests/unit/core/test_non_canonical_exemptions.py`；`467 passed` = 同名保护下 `tests/unit/core/{test_session_catalog_store,test_session_control_store,test_session_creation,test_thread_creation}.py`。

**第二轮曾未能复跑的项（2026-10-01 已收敛）**：`tests/unit/core/test_session_catalog_migration.py` 第二轮复跑为 67 failed / 21 passed（EXIT=1），失败原因为 `app/core/session_catalog_migration/_journal.py` 的 `NameError: name '_atomic_write_bytes' is not defined`——该模块当时正被并发 agent 从单文件重构为包（`app/core/session_catalog_migration{,.py}` 并存于工作树，未提交），属他人**在途**改动。**该在途重构现已合并**：单文件 `app/core/session_catalog_migration.py` 已物理下线、包版 `_journal.py:10` 已 `from app.core.atomic_fs import atomic_write_bytes as _atomic_write_bytes`；独立复核 2026-10-01 复跑 `timeout 1200 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run --no-sync pytest -q tests/unit/core/test_session_catalog_migration.py -p no:randomly` → **88 passed in 19.06s（EXIT=0）**。失败项已闭合；但 6.4 的「子门槛」仍按上文口径（报告缺路径/建议动作）保持未完全达。
