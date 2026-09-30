## 1. 显式依赖与生成来源（D1）

- [ ] 1.1 在 `pyproject.toml` 的 `dependencies` 中显式新增 `uuid-utils>=0.16`，运行 `uv sync` 并确认 `uv.lock` 中 `uuid-utils` 仍是同一锁定版本 `0.16.0`。门槛：`uv sync` 退出码 0；`uv run python -c "import uuid_utils; print(uuid_utils.__version__)"` 退出码 0 且输出 `0.16.0`。
- [ ] 1.2 把 `app/core/identifier.py` 的 `create_uuid_hex()` 改为返回 `uuid_utils.uuid7().hex`；保留 `create_prefixed_id(prefix)` 的 `f"{prefix}_{hex}"` 外形。门槛：`uv run python -c "from app.core.identifier import create_uuid_hex; h=create_uuid_hex(); assert h[12]=='7' and h[16] in '89ab' and len(h)==32"` 退出码 0。
- [ ] 1.3 增加 fail-closed 守卫：`uuid_utils` 导入失败或 `uuid7` 不可用时抛出详细错误，MUST NOT 回退 `uuid.uuid4()`。门槛：单测模拟导入缺失，断言抛错且无 v4 产出（`uv run pytest -q tests/unit/core/test_identifier.py` 退出码 0）。

## 2. 单调性与时钟回拨（D2、D4c）

- [ ] 2.1 为唯一工厂补充同毫秒单调测试：同进程内同一毫秒连续生成 20000 个 id，断言按 hex 排序与生成顺序逐字节一致且全部唯一。门槛：`uv run pytest -q tests/unit/core/test_identifier_uuidv7_monotonic.py` 退出码 0。
- [ ] 2.2 补跨毫秒自然单调测试：连续生成 200000 个 id，断言全局有序且唯一。门槛：同一测试文件退出码 0。
- [ ] 2.3 实现并测试时钟回拨钳制：注入早于上次生成时刻的时间源时，新 id MUST 不小于上一次。门槛：单测断言回拨场景下非递减，退出码 0。
- [ ] 2.4 断言生成路径不传显式 `timestamp`（可静态检查或断言调用形态），并在代码注释中说明显式时间戳会破坏同毫秒单调。门槛：对应单测退出码 0。
- [ ] 2.5（A3 量化）补一条量化上界测试：同一毫秒内生成 500000 个 id，断言同毫秒组最大规模被实测记录（约 3710）且组内全部有序唯一；并断言实现与文档只承诺「同进程内同毫秒非递减且唯一」+「跨进程共享 48 bit 毫秒分辨率」，不承诺跨进程同毫秒有序。门槛：对应测试退出码 0。

## 3. 校验层正名与单一 profile（D5）

- [ ] 3.1 把 `app/core/session_catalog_store.py` 的 `_validate_uuid_v4_payload` 正名为 `_validate_uuid_payload`，`_UUID_VERSION_HEX_INDEX` 语义改为要求 `version == 7`；删除任何 `v4` 命名残留；更新 `validate_session_id`/`validate_thread_id` 的 docstring 与注释为「UUIDv7 位 profile」。门槛：`uv run python -c "import app.core.session_catalog_store as s; print([n for n in dir(s) if 'uuid' in n.lower()])"` 退出码 0 且输出无 v4 命名。
- [ ] 3.2 同步 `app/protocol/canonical.py` 的注释（现写「payload 第 13 个 hex 位为 4（UUIDv4 version）…」）。门槛：`rg -n 'UUIDv4|非 v4|v4 bit' app/core/session_catalog_store.py app/protocol/canonical.py` 退出码 1（0 命中）。
- [ ] 3.3 补负向测试：payload 第 13 个 hex 为 `4` 的 id MUST 被拒绝。门槛：`uv run pytest -q tests/unit/core/test_canonical_identifier_matrix.py` 退出码 0。
- [ ] 3.4 更新 `tests/unit/core/test_canonical_identifier_matrix.py` 的 docstring（现写「非 v4 bits」）与 `make_session_id`/`make_thread_id`（现用 `uuid.uuid4().hex`）为 v7 生成。门槛：`rg -n 'uuid4' tests/unit/core/test_canonical_identifier_matrix.py` 退出码 1。

## 4. 日期桶与 id 内嵌时间戳一致性（D4）

- [ ] 4.1 在 `validate_storage_relative_locator()` 中加入「`sessions/YYYY/MM/DD` 的 UTC 日期 == id 内嵌 48 bit 毫秒时间戳的 UTC 日期」断言；不一致抛显式完整性错误。门槛：`uv run pytest -q tests/unit/core/test_session_catalog_store.py` 退出码 0。
- [ ] 4.2 补负向测试：构造「分桶日期与 id 内嵌时间戳不一致」的 locator，断言 fail-closed 且不扫盘、不改桶。门槛：同一测试文件退出码 0。
- [ ] 4.3 确认 child thread 的 `threads/YYYY/MM/DD/{thread_id}`（`app/core/session_control_store.py`）同样按 UTC 且与 id 内嵌时间戳一致。门槛：`uv run pytest -q tests/unit/core/test_thread_creation.py` 退出码 0。
- [ ] 4.4（A3）实现「回拨与分桶互不冲突」语义：创建流程用同一已钳制时间源 `effective_created_ms = max(monotonic_now_ms, last_issued_ms)` 同时推出 id 内嵌时间戳与分桶 UTC 日期。门槛：新增测试断言「注入回拨后创建 session，其分桶日期与 id 内嵌时间戳一致且不报错」，退出码 0。
- [ ] 4.5（A3）断言默认语义下回拨 MUST NOT 变成用户可见故障（不拒绝创建）；若实现选择「拒绝并报告」的显式配置语义，MUST 有对应测试并在文档判死二选一。门槛：`uv run pytest -q <该测试>` 退出码 0。
- [ ] 4.6（A3）量化断言的负向测试：断言文档/实现不声称跨进程同毫秒有序或主键严格按时间相邻。门槛：对应断言测试退出码 0。

## 5. SQLite 主键与索引（D6）

- [ ] 5.1 断言 `nodes.node_id`、`thread_catalog.thread_id` 的表 DDL 未因 v7 变更（无新列、无新索引、无迁移 DDL）。门槛：`uv run pytest -q tests/unit/core/test_session_catalog_store.py tests/unit/core/test_session_control_store.py` 退出码 0。
- [ ] 5.2 补测试：v7 id 插入既有主键表后，按 id 排序≈按时间顺序。门槛：对应单测退出码 0。

## 5A. 阻断性前置：哈希与幂等键审计（D9，MUST 在 §6 之前完成）

- [x] 5A.1 审计全仓 `content_hash` / `contribution_content_hash` / `plan_hash`(`context_plan_hash`) / `request_hash`(`context_request_hash`) / itemized plan-hash / 幂等键 / 去重键的输入，逐条判定是否**直接或间接**包含 `session_id` / `thread_id` / 资源 id（含 `create_prefixed_id` 产物、`display_uri`、`entry_identity`、catalog payload）。门槛：审计报告落盘到 `out/tests/temp/uuidv7_openspec/artifacts/`，命令 `rg -rn 'sha256_jcs|hashlib.sha256|idempotency_key' app --glob '*.py' -l` 退出码 0 且报告覆盖全部命中文件。
- [x] 5A.2 命中清单必须含「文件 + 符号 + 判定依据」；已实测命中至少包括 `app/domain/itemized/hash/plan_hash.py` 的 `context_plan_hash`（含 `session_id`/`plan_id`/`ref_id`）、`app/domain/itemized/hash/request_hash.py` 的 `context_request_hash`、`app/core/thread_creation.py` 的 `compute_thread_creation_preimage_hash`（含 `session_id`/`thread_id`）、以及 `app/services/infrastructure/rollout_context/storage/transaction.py` 的 `default_idempotency_key(commit_kind, subject_id, outcome, metadata)`（**签名显式含 `subject_id`，生产调用点即 canonical id**）。门槛：`rg -n 'session_id|thread_id|subject_id' app/domain/itemized/hash/plan_hash.py app/core/thread_creation.py app/services/infrastructure/rollout_context/storage/transaction.py` 退出码 0，且报告逐条记录。
- [x] 5A.2b A5 强制处置：`default_idempotency_key` 已判定**会漂移**，MUST 进 §5A 的处置表并明确「随迁移一致重算」或「该 id 不参与迁移」，MUST NOT 只登记不处置。门槛：报告处置表中存在该符号条目且处置非空。
- [x] 5A.3 必须附「已验证**不**含 id」的**负向证据**，例如 `app/domain/itemized/hashing.py` 的 `content_hash` 输入仅 `{payload_kind, payload}`、`contribution_content_hash` 仅 `{contribution_kind, body}`，`app/core/session_creation.py` 的 `compute_session_creation_preimage_hash` 四元组 `{workspace_id, parent_node_id, title, session_metadata}` 不含 `session_id`。门槛：`rg -n 'def content_hash|def contribution_content_hash' app/domain/itemized/hashing.py` 退出码 0 且报告记录输入字段清单。
- [x] 5A.4 逐条给出处置：以 id 为输入的哈希/幂等键 MUST 明确为「随迁移一致重算」或「该 id 不参与迁移」，并配验证；MUST NOT 留成「迁移后哈希漂移但无人负责」。门槛：报告逐条标注处置；未落定条数 MUST 为 0。
- [ ] 5A.5 审计未落定前，§6 的迁移任务 MUST NOT 执行任何重编号。门槛：执行记录证明 §6 在 §5A 全部勾选后才开工。
> **（2026-09-30 补齐，5A.1–5A.4 已满足；5A.5 留作 §6 开工时的执行记录）**：交付物已补齐到 `out/tests/temp/uuidv7_openspec/artifacts/hash_audit_*`（源报告 `out/tests/temp/uuidv7_hash_audit/artifacts/`）。门槛命令 `rg -rn 'sha256_jcs|hashlib.sha256|idempotency_key' app --glob '*.py' -l` 实测 **191 个文件全覆盖、未归类 = 0**（三 token 各自命中 56/81/102，并集 191）；互斥归类为 A 原语本体 3、B 内容哈希 39、C canonical 身份原型 2、D opaque 56、E 非 canonical id 派生 23、F 含 canonical id 的 preimage 68。**F 类 68 个文件逐符号处置、未落定 = 0**（随迁移一致重算 66、fork 链路 key 重映射重算 1、该 id 不参与迁移 1）。负向证据齐：`content_hash` 仅 `{payload_kind, payload}`、`contribution_content_hash` 仅 `{contribution_kind, body}`、`compute_session_creation_preimage_hash` 仅 `{workspace_id, parent_node_id, title, session_metadata}`，另附 17 条「只吃正文/默认值」负向。
>
> **§5A 已落定的两条边界，供 §6 直接复用**：① `default_idempotency_key(commit_kind, subject_id, outcome, metadata)` 的 `subject_id` 即 canonical id，判定**会漂移**，处置 = 随迁移一致重算（实测不同 subject_id 得到不同 key）；② `compute_thread_creation_preimage_hash` 含 session_id/thread_id/delegation_id，处置 = 随迁移一致重算（实测 v4-like 与 v7-like 摘要不同）。唯一「该 id 不参与迁移」是 `core/session_subtree_delete.py` 的 `idempotency_key`（瞬时隔离目录名，随删除回收，非持久身份）。
>
> **迁移面口径（owner 裁定）**：本审计按「是否含 canonical id（**含其派生面**）」判定，把由 message_id/turn_id/execution_id 派生、或经 `default_idempotency_key(subject_id=...)` 携带 canonical id 的哈希/幂等键一并纳入「重算」。**§6 不得收窄为只重编号 `ses_`/`thr_` 字面**，否则本报告的间接派生项会变成「迁移后漂移但无人负责」。本项门槛满足。

## 5B. 阻断性前置：gateway 控制面库逐表分类（D10，MUST 在 §6 之前完成）

- [x] 5B.1 对控制面库（`app/gateway/control/gateway_state.py` 等）承载 session 身份的表逐表分类为 `migrate` / `explicitly_invalidated` / `not_affected`。**已实测控制面只有两个 SQLite 库**：库 A `gateway.sqlite`（`app/gateway/main.py` 实例化，建表权威在 `gateway_state.py` 的 `_GATEWAY_MIGRATIONS`，21 张表 + 框架表 `schema_migrations`）、库 B `federation/control.sqlite`（`app/gateway/federation/store.py`，4 张表）。门槛：报告给出逐表分类与判定依据，且 `rg -n 'session_id|access_session_id' app/gateway/control/gateway_state.py` 退出码 0。
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

- [ ] 6.1 按 design D12 的「工厂前缀 × 持久面」全集矩阵（`IdentifierPrefix` 的 33 个前缀）枚举迁移面，MUST NOT 只匹配 `ses_`/`thr_` 字面。门槛：矩阵落盘到 `out/tests/temp/uuidv7_openspec/artifacts/`；命令 `rg -n 'IdentifierPrefix = Literal' -A40 app/core/identifier.py` 退出码 0 且矩阵行数 MUST 等于该 `Literal` 的前缀数（33）；每个持久面前缀 MUST 给出具名载体证据，每个非持久面前缀 MUST 给出「不落盘」的负向证据。
- [ ] 6.1b 复核已实测的持久面漏项至少覆盖 `op_`（`navigation_mutation_records` 主键）、`strm_`（`message_streams/*.jsonl` 文件名）、`msg_`（rollout `messages.message_id`）、`evt_`/`snapshot_`（message_stream JSONL）、`part_`（`item_parts.part_id`）、`goal_`（`goal.json`）、`gen_`/`grun_`（generators 文件）、`team_`/`ttask_`/`tevt_`（team JSON/JSONL），**外加 2026-09-30 控制面审计新点的四处非 SQLite 持久面**：控制面会话索引缓存 `state/gateway/indexes/session-catalogs/*.json`（含真实 `ses_`/`thr_`）、用户档案 `state/gateway/users/<id>/profile.jsonc` 的 `session_sidebar.collapsed_session_ids`（含 canonical `ses_`）、Gateway generators 产出的 `state/gateway/generators/*.json` 与 `generation-runs/**`（含 `placement.session_id`/`message_id`/`job_id`）、工作区后端持久面（`workspace_activity.session_id`、`attachment_*.owner_session_id`）。门槛：报告逐项列出具名路径与调用点。**注意：迁移 generators 的产出数据文件不等于修改 `app/gateway/control/generators.py` 源码，后者为受保护路径，全程禁改。**
- [ ] 6.2 复用 `app/core/session_catalog_migration.py` 的 staging + journal + 隔离区形态，实现一次性、可恢复、带 source→target lineage 账本的 v4→v7 重编号迁移。门槛：`uv run pytest -q tests/unit/core/test_session_catalog_migration.py` 退出码 0。
- [ ] 6.3 补迁移中断恢复测试与「无法归属即 fail-closed/隔离、不扫盘吸收」测试。门槛：对应迁移测试退出码 0。
- [ ] 6.4 迁移完成后把校验器收紧为只接受 v7，并断言运行路径无 v4 双读、无旧 ID path alias。门槛：`rg -n 'v4|uuid4' app/core/session_catalog_store.py` 退出码 1；迁移收敛测试退出码 0。
- [ ] 6.5（A2 方案 a）实现唯一维护开关 `identity_profile_migration_active`：开启时校验器接受 `v4|v7`（唯一允许双接受的时刻），关闭时只接受 `v7`；账本终态事务提交后 MUST 在同一次维护操作内把开关置为关闭。门槛：单测断言「开关 true → v4/v7 均接受」「开关 false → 仅 v7，v4 被拒绝」，`uv run pytest -q <该测试>` 退出码 0。
- [ ] 6.6（A2）实现启动期版本闸门：某工作区处于迁移窗口（开关为 true）时，旧代码版本 MUST 拒绝服务该工作区，MUST NOT 新旧代码并行。门槛：对应测试断言旧版本启动被拒，退出码 0。
- [ ] 6.7（A2）收敛断言：开关关闭后，必须有一条测试证明「`v4` 位 profile 的 canonical 身份被拒绝且 `v7` 被接受」，以机械证明窗口期已结束。门槛：该测试退出码 0。

## 7. JS 服务进程与浏览器前端边界（D7）

- [ ] 7.1 在 `src/workspace-services/{browser,terminal}/server/` 与 `src/clients/web/src/utils/media/mediaAttachments.ts` 的 id 生成点上方加中文注释，显式声明这些是非 canonical 身份、允许使用 v4，并说明原因（Node 无 `randomUUIDv7`；浏览器无 `Bun.*`）。门槛：`rg -n '非 canonical' src/workspace-services src/clients/web/src/utils/media/mediaAttachments.ts` 退出码 0。
- [ ] 7.1c 把运行时非工厂前缀豁免纳入正名后的枚举：`runtime_lease_<uuid4>`（`app/gateway/control/gateway_state.py`）与 `target_generation_<uuid4>`（Gateway 目标生成标识）经 2026-09-30 实测确认**不是 `IdentifierPrefix` 工厂前缀**，与 §7.1b 的豁免集同属「非 canonical 身份」，MUST 在豁免枚举中显式登记，MUST NOT 被当作 v4 残留误列入迁移面。门槛：报告给出具名载体与生成点，且豁免枚举测试覆盖这两者。
- [ ] 7.2 补断言：canonical 校验器 MUST 拒绝这些非 canonical id 作为 session/thread 身份。门槛：对应单测退出码 0。
- [ ] 7.3 若前端 UI 改动，执行 `bun run --cwd src/clients/web build`。门槛：退出码 0。

## 8. 边界与引用（D8）

- [x] 8.1 确认本 change 未定义/改写 VRN、scope、kind、拒绝码或 ResourceIdentity；引用处均为具名 change 名。门槛：`rg -n 'VRN|scope_id|拒绝码' openspec/changes/migrate-identifiers-to-uuidv7/specs` 仅出现引用语境。 **（本次实测（提交 `f6fc990f`）现状：`rg -n 'VRN|scope_id|拒绝码' openspec/changes/migrate-identifiers-to-uuidv7/specs` 退出码 0，仅 3 处命中，全部为「本 capability 不定义 VRN/scope_id/拒绝码，只具名引用」的引用语境（`spec.md:3`、`:121` 的 requirement 标题、`:123`）；门槛满足。）**
- [x] 8.2 在 design D11 点名四处「仍含 v4 表述、需由各自 owner 收口」的位置（文件 + capability + requirement/任务）：`add-itemized-rollout-context` 的 `specs/itemized-rollout-context/spec.md`（capability `itemized-rollout-context`，requirement「产品 Session、durable Thread 与 LangGraph namespace 必须严格分层」）与 `specs/rollout-checkpoint-storage/spec.md`（capability `rollout-checkpoint-storage`，requirement「rollout storage 必须以 SessionThread 为物理与事务 owner」）；`add-context-injection-lifecycle` 的 `specs/context-injection-lifecycle/spec.md`（capability `context-injection-lifecycle`，requirements「Context lifecycle owner 必须精确为 SessionThread」「生命周期场景必须进入统一 Web E2E 验收模块」）与 `tasks.md`（任务 2.1）；声明本 change 是 id 生成位 profile 唯一 owner、上述文本兑现时 MUST 引用本 change、MUST NOT 复述取值；本 change MUST NOT 代改。门槛：报告列出具名路径与冲突文本。 **（本次实测（提交 `f6fc990f`）现状：D11 已交付四处具名路径（`design.md:160-171`：itemized 的 `specs/itemized-rollout-context/spec.md`、`specs/rollout-checkpoint-storage/spec.md`；CIL 的 `specs/context-injection-lifecycle/spec.md`、`tasks.md` 任务 2.1）；复跑 `rg -n '第 13 个 hex|UUIDv4|v4 bit profile'` 对上述四处文件零命中，四处 v4 表述均已由各自 owner 收口引用本 change；门槛「报告列出具名路径与冲突文本」满足，本 change 未代改。）**
- [x] 8.3 核验四方 owner 已自行在其产物中收口并引用本 change（本 change MUST NOT 代改这四处）。门槛：`rg -n 'migrate-identifiers-to-uuidv7' openspec/changes/add-itemized-rollout-context openspec/changes/add-context-injection-lifecycle` 退出码 0（由 owner 侧提交达成，本 change 只核验）。 **（本次实测（提交 `f6fc990f`）现状：`rg -n 'migrate-identifiers-to-uuidv7' openspec/changes/add-itemized-rollout-context openspec/changes/add-context-injection-lifecycle` 退出码 0，命中 14 行（itemized: `tasks.md:100/143`、`design.md:745/1000`、`proposal.md:34`、`specs/itemized-rollout-context/spec.md:15/76`、`specs/rollout-checkpoint-storage/spec.md:231`、`specs/session-turn-history/spec.md:139`；CIL: `tasks.md:13/116`、`design.md:636`、`specs/context-injection-lifecycle/spec.md:721/1038`）；门槛满足。）**
- [x] 8.4 在四方收口完成后重跑 `openspec validate --strict --all`。门槛：输出 `0 failed` 且退出码 0。 **（本次实测（提交 `f6fc990f`）现状：`openspec validate --strict --all` 输出 `Totals: 40 passed, 0 failed (40 items)`，退出码 0；门槛满足。）**

## 9. 质量门

- [x] 9.1 `openspec validate migrate-identifiers-to-uuidv7 --strict` 输出 `Change 'migrate-identifiers-to-uuidv7' is valid`，退出码 0。 **（本次实测（提交 `f6fc990f`）现状：`openspec validate migrate-identifiers-to-uuidv7 --strict` 输出 `Change 'migrate-identifiers-to-uuidv7' is valid`，退出码 0；门槛满足。）**
- [x] 9.2 `openspec validate --strict --all` 退出码 0 且 `0 failed`（本 change 加入后为 40 passed）。 **（本次实测（提交 `f6fc990f`）现状：`openspec validate --strict --all` 输出 `Totals: 40 passed, 0 failed (40 items)`，退出码 0；门槛满足。）**
- [ ] 9.3 实施阶段的完整测试带进程外保护执行（按 AGENTS.md：`bun run test:matrix -- --suite=<id>` 或 `timeout <秒> bash -c 'ulimit -d 4194304; exec "$@"' bash <命令>`），退出码 0。
