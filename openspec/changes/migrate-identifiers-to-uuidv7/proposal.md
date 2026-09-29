## Why

项目当前所有 canonical 标识符（`session_id`、`thread_id` 及其它由 id 工厂生成的持久身份）都是 **UUIDv4 位 profile**：`app/core/identifier.py` 的 `create_uuid_hex()` 返回 `uuid.uuid4().hex`，`app/core/session_catalog_store.py` 的校验器把 payload 第 13 个 hex 硬性要求为 `4`（注释与 docstring 逐字写「UUIDv4 位 profile」「非 v4 位 profile 一律直接拒绝」）。这使得：

- **按时间分桶的目录无法由 id 自校验**：`sessions/YYYY/MM/DD/{session_id}` 的日期桶当前由创建时刻**独立记账**（`session_catalog_store.py` 以 `created_at.astimezone(UTC).date()` 生成 locator），id 本身不携带任何时间信息，因此「分桶与 id 是否一致」只能靠另存的 `created_at` 复核，无法从 id 直接推导或反查。
- **SQLite 主键没有时间局部性**：`nodes.node_id`、`thread_catalog.thread_id` 等以 id 文本作 `TEXT PRIMARY KEY`，随机 v4 让 B-tree 插入散布在整个页空间，缺乏时间序带来的写入局部性。

UUIDv7（RFC 9562）前 48 bit 承载 Unix 毫秒时间戳，正好让 id 自带时间序：分桶可由 id 内嵌时间戳推导并复核，主键获得时间局部性。本 change 把项目所有 UUIDv4 身份统一改为 UUIDv7，并把它作为 canonical id profile 的**唯一 owner**。

## What Changes

- **BREAKING**：canonical 标识符的位 profile 从 UUIDv4 改为 **UUIDv7**。`app/core/identifier.py` 的唯一 id 工厂产出 v7 hex；`app/core/session_catalog_store.py` 的校验器（现名 `_validate_uuid_v4_payload`，硬要求 `payload[12] == "4"`）**正名并改为只接受 v7 位 profile**，不再存在第二套 v4 分支。
- **拒绝双轨**：**MUST NOT** 长期同时接受 v4|v7 两种位 profile。存量 v4 身份按「**一次性显式迁移**」方向处理：在受维护窗口与备份约束下，用可恢复账本把权威身份重编号为 v7 并保留 lineage；迁移窗口结束后校验器**只接受 v7**。**MUST NOT** 提供旧 ID path alias，**MUST NOT** 双读，**MUST NOT** 扫盘重建。
- **生成来源定稿为显式直接依赖 `uuid-utils`**：本机 Python 3.12.3 无 `uuid.uuid7()`（实测），stdlib v7 需 Python 3.14，而发行包与 Docker 运行时固定为 3.12（`packaging/runtime/versions.mjs`、`tools/cross-platform-development-targets/docker/Dockerfile`），抬高到 3.14 代价过大，故否决 stdlib 方案。`uuid-utils` 已在 `uv.lock` 作**传递依赖**（langchain-core/langsmith）存在且已是锁定版本 `0.16.0`，本 change 必须把它提升为 `pyproject.toml` 的**显式直接依赖**，MUST NOT 依赖「某个第三方包偶然传递存在」。缺失或不可用时 **fail-closed**，绝不回退到 v4。
- **单调性合同**：v7 的「时间有序」是本 change 的全部价值，故 MUST 明确：同一进程内、同一毫秒内的 id **MUST 非递减且唯一**（用 `rand_a` / 计数器方案）；跨进程/跨重启只保证 **48 bit 毫秒分辨率**的时间序。MUST NOT 传入显式时间戳破坏单调（实测 `uuid-utils` 传显式 `timestamp=` 时同毫秒内**不再单调**）。
- **日期桶由 id 自推导且可校验**：`sessions/YYYY/MM/DD/{session_id}` 的日期 MUST 与 id 内嵌 48 bit 毫秒时间戳按 **UTC** 推导出的日期一致；不一致即 fail-closed 完整性错误。时钟回拨（NTP 校时）行为 MUST 显式规定为「进程内非递减钳制」。
- **校验层正名**：`_validate_uuid_v4_payload` / `_UUID_VERSION_HEX_INDEX` 一类把 `v4` 写进名字的标识、注释与 docstring MUST 一并正名为与「当前 profile」一致的单一名词，**MUST NOT** 同概念异名或保留第二套校验函数。
- **JS / 前端边界必须诚实声明**：`src/workspace-services/**` 的 browser/terminal 后端进程实际由 **Node** 启动（`BOXTEAM_NODE_BIN`，见 `app/gateway/runtime/process.py`），Node 22 **无** `randomUUIDv7`（实测）；`src/clients/web` 是浏览器构建产物，**没有** `Bun.*`。故：Node 服务进程与浏览器前端**拿不到原生 v7**，其生成的 id（`term_` / `browser_` / `screenshot_` / `page_` / `inline:` 附件 file id 等）MUST 被显式声明为**非 canonical 身份**并允许继续使用 v4；**MUST NOT** 假装两者已统一。
- **显式边界**：本 change 是 **id 生成位 profile** 的唯一 owner，**不是** VRN / ResourceIdentity 的 owner。VRN/ResourceIdentity 是「不可解析身份」，本 change 改的是「生成位 profile」，两者 MUST NOT 混为一谈；具名引用在途 change。

## Capabilities

### New Capabilities
- `uuidv7-identifier-profile`: canonical 标识符的 UUIDv7 位 profile、唯一 id 工厂与其显式直接依赖、生成来源的 fail-closed 行为、同进程内同毫秒的非递减与唯一性合同、`sessions/YYYY/MM/DD` 日期桶与 id 内嵌时间戳一致性（UTC）、时钟回拨行为、SQLite 主键/索引不重建的裁定、校验层正名的单一 profile 形态、JS 服务进程与浏览器前端的 v7 可用性边界与非 canonical 身份的显式豁免、存量 v4 身份一次性显式迁移（lineage、不双读、不留 alias、不扫盘）。

### Modified Capabilities
<!-- 无：本轮实测确认 `openspec/specs/**` 下**没有任何已发布 requirement** 承载 canonical id 位 profile 或 `sessions/YYYY/MM/DD` 日期桶与 UTC 推导义务（对 `UUIDv4`、`bit profile`、`ses_[0-9a-f]`、`payload 第 13 个 hex` 等关键字全仓扫描 0 命中）。承载该义务的文本目前只存在于**在途 change 的 delta**（`add-itemized-rollout-context` 与 `add-context-injection-lifecycle`），故本 change 按「如实写 New」处理，并把两处在途 delta 的收口列为待 owner 处理的引用项（本 change 不代改）。 -->

## Impact

- **规划产物**：新增 `openspec/changes/migrate-identifiers-to-uuidv7/` 的 `proposal.md` / `specs/uuidv7-identifier-profile/spec.md` / `design.md` / `tasks.md`。
- **受影响系统（实施阶段，不在本 change 落地）**：
  - 唯一 id 工厂：`app/core/identifier.py`（`create_uuid_hex` / `create_prefixed_id`）。
  - 校验与分桶：`app/core/session_catalog_store.py`（`_validate_uuid_v4_payload`、`_UUID_VERSION_HEX_INDEX`、`validate_session_id`、`validate_thread_id`、`validate_storage_relative_locator`、locator 构建）、`app/core/session_control_store.py`（child thread locator 构建）、`app/core/session_control_thread_catalog/thread_catalog.py`。
  - 协议层形态常量：`app/protocol/canonical.py`。
  - 依赖：`pyproject.toml` / `uv.lock`（把 `uuid-utils` 提升为显式直接依赖）。
  - 生产侧散落 `uuid.uuid4()`：实测 `app` 下约 45 个 `.py` 文件含直接 `uuid4`/`UUID(` 调用（示例：`app/core/workspace_identity.py`、`app/gateway/registry.py`、`app/core/trace_middleware.py`、`app/core/session_catalog_resolver.py`、`app/agents/context_checkpoint_store.py`），需按「canonical 身份 vs 非持久 id」分类迁移。
  - 一次性迁移机器先例：`app/core/session_catalog_migration.py`（staging + ledger + 隔离区 fail-closed 形态可复用）。
  - 测试：`tests/unit/core/test_canonical_identifier_matrix.py`（docstring 写「非 v4 bits」、`make_session_id`/`make_thread_id` 用 `uuid.uuid4().hex`）等约 44 个测试文件含 v4 生成点。
  - JS：`src/workspace-services/browser/server/*.js`、`src/workspace-services/terminal/server/*.js`（Node 运行时 `randomUUID`）、`src/clients/web/src/utils/media/mediaAttachments.ts`（浏览器 `crypto.randomUUID()`）。
- **依赖与 owner**：`uuid-utils>=0.16` 必须成为显式直接依赖。VRN 语法、scope 闭集、kind 闭集与拒绝码归 `add-unified-virtual-resource-addressing`；会话上下文寻址归 `migrate-session-context-uri-to-vrn`；工作区身份与 `scope_id` 推导归 `add-multi-workspace-backend-mounting`；资源身份/VRN 持久化归 `add-workspace-persistent-resource-management`。本 change 只拥有 id **生成位 profile**，只具名引用，不复述、不自造。
- **不做**：不引入第二套 id 语法；不把 revision/hash 编码进 id；不长期同时接受 v4|v7；不提供旧 ID path alias；不扫盘重建 catalog；不改动 VRN/ResourceIdentity 的定义；不伪造「浏览器与 Node 服务进程已经产出 v7」。

