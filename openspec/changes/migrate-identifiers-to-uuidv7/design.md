## 背景

本 change 只改 id 的**生成位 profile**：让 canonical 标识符自带时间序，从而（a）`sessions/YYYY/MM/DD` 分桶可由 id 复核，（b）B-tree 主键获得时间局部性。塑造方案所需的现状与约束如下（均为本轮实测）。

**现状与事实源**

- 唯一 id 工厂：`app/core/identifier.py` 的 `create_uuid_hex()` 返回 `uuid.uuid4().hex`；`create_prefixed_id(prefix)` 返回 `f"{prefix}_{hex}"`。`IdentifierPrefix` 是闭合 `Literal`（含 `ses`、`thr`）。
- canonical 校验器（名里带 v4）：`app/core/session_catalog_store.py` 有 `_UUID_VERSION_HEX_INDEX = 12`（要求 `payload[12] == "4"`）、`_UUID_VARIANT_HEX_INDEX = 16`、`_UUID_VARIANT_HEX_CHARS = "89ab"`、`_validate_uuid_v4_payload()`，被 `validate_session_id()`（`ses_`）与 `validate_thread_id()`（`thr_`）调用；注释与 docstring 逐字写「UUIDv4 位 profile」「非 v4 位 profile 一律直接拒绝，不得清洗或截断」。
- 分桶由创建时刻独立记账：`session_catalog_store.py` 用 `utc_date = created_at.astimezone(UTC).date()` 与 `locator = f"sessions/{utc_date:%Y/%m/%d}/{allocated_session_id}"` 生成 locator；`validate_storage_relative_locator()` 只校验形态与「日期真实存在」，**不**与 id 内嵌时间做任何比对。`app/core/session_control_store.py` 对 child thread 同样以 `created_at.astimezone(UTC)` 构建 `threads/{utc_date:%Y/%m/%d}/{child_thread_id}`。
- SQLite 主键：`nodes.node_id TEXT PRIMARY KEY`、`thread_catalog.thread_id TEXT PRIMARY KEY`，均以 id 文本作主键。
- 依赖：`pyproject.toml` 的 `requires-python = ">=3.11"`，`dependencies` **无任何 uuid 库**；`uv.lock` 中 `uuid-utils 0.16.0` 仅作为 langchain-core/langsmith 的**传递依赖**出现。本机 `uv run python` 实测为 Python 3.12.3，`hasattr(uuid, "uuid7") == False`。发行包运行时（`packaging/runtime/versions.mjs`）与 Docker（`tools/cross-platform-development-targets/docker/Dockerfile`）均固定 Python 3.12。`uuid_utils` wheel 覆盖 cp311–cp314。
- JS：`Bun.randomUUIDv7` 在 bun 1.3.11 存在；Node 22.17 **无** `crypto.randomUUIDv7`。`src/workspace-services/{browser,terminal}/server/*.js` 用 `import { randomUUID } from "node:crypto"`；`src/clients/web/src/utils/media/mediaAttachments.ts` 用浏览器 `crypto.randomUUID()`。`app/gateway/runtime/process.py` 以 `BOXTEAM_NODE_BIN` 启动辅助服务（Node，非 bun）。
- 规模：`app` 下约 45 个 `.py` 文件含直接 `uuid4`/`UUID(` 调用，`tests` 下约 44 个；`src` 下 8 个 JS/TS 文件含 `randomUUID`。
- 已发布 spec 零承载：`openspec/specs/**` 对 `UUIDv4`、`bit profile`、`ses_[0-9a-f]`、`payload 第 13 个 hex` 等关键字扫描 0 命中；只有 `scalable-session-navigation` 出现 `sessions/` 字样，但无日期桶与 UTC 推导义务。承载 v4 位 profile 义务的文本只存在于**在途 change 的 delta**（`add-itemized-rollout-context` 的 `specs/itemized-rollout-context/spec.md` 与 `specs/rollout-checkpoint-storage/spec.md`、`add-context-injection-lifecycle` 的 `specs/context-injection-lifecycle/spec.md` 与 `tasks.md`）。

## 目标与非目标

**目标**

- 把 canonical 标识符位 profile 从 v4 统一为 v7，并作为该 profile 的唯一 owner。
- 定稿生成来源、单调性、fail-closed 与迁移范围，使「时间有序」这一价值主张可被验证。
- 让 `sessions/YYYY/MM/DD` 分桶从「独立记账」变为「可由 id 复核」。
- 诚实声明各运行面的 v7 可用性边界。

**非目标**

- 不定义 VRN / ResourceIdentity / scope / 拒绝码（归 `add-unified-virtual-resource-addressing` 等）。
- 不实现任何生产代码；本 change 只产出规划产物。
- 不把 revision/hash 编码进 id；不引入第二套 id 语法。
- 不为浏览器与 Node 服务进程伪造 v7 能力。

## 决策

### D1：生成来源 = 显式直接依赖 `uuid-utils>=0.16`

**决定**：canonical 身份的 v7 payload 由 `uuid_utils.uuid7().hex` 生成；`pyproject.toml` MUST 显式声明 `uuid-utils>=0.16` 为直接依赖；缺失即 fail-closed。

**理由**：

- stdlib `uuid.uuid7()` 需 Python 3.14，而本机与发行包/Docker 运行时均为 3.12（实测 `hasattr(uuid,'uuid7') == False`）；抬到 3.14 会同时改动 `requires-python`、锁定运行时与全部打包链路，代价远大于新增一个已在锁定图中的依赖。
- `uuid-utils 0.16.0` 已在 `uv.lock` 存在（langchain-core/langsmith 传递依赖），wheel 覆盖 cp311–cp314，与本仓 `requires-python>=3.11` 兼容；把它提升为直接依赖只需在 `pyproject.toml` 声明，不改运行时版本。
- MUST NOT 依赖「传递存在」：传递依赖可被上游升级移除，必须显式声明。

**备选**：

- *(i) stdlib `uuid.uuid7()` + 抬 `requires-python` 到 3.14*：否决，代价过大且要改发行包/Docker/CI 全部 Python 版本。
- *(iii) 自实现 RFC 9562 UUIDv7*：否决，重复造轮子（违反「优先第三方库」），且要自行承担单调性、时钟回拨与并发正确性。

### D2：单调性 = 同进程内同毫秒非递减且唯一，跨进程只保证毫秒分辨率

**决定**：同进程同毫秒 MUST 非递减且唯一（`rand_a`/计数器方案）；跨进程/跨重启仅保证 48 bit 毫秒时间序；生成路径 MUST NOT 传显式时间戳。

**理由**：

- 实测 `uuid_utils.uuid7()` 默认路径：200000 个自然生成 id 全局按 hex 有序且唯一；8 线程各 2000 个，每线程局部有序且全局有序。
- 实测**传入显式 `timestamp=`** 时同毫秒内**不再单调**（8 个同 `timestamp` 值按 hex 序无序）。故允许的注入面必须与生产保持同一单调合同，生产路径 MUST NOT 传显式时间戳。
- Bun 的 `Bun.randomUUIDv7()` 实测结果**不稳定**：200000 个样本多数运行全局有序，但部分运行出现少数同毫秒组非单调——说明「跨实现不得假定严格单调」，本 capability 只对受控的 Python 工厂给强保证。

**备选**：

- *(a) 使用纯随机 v7*：否决，同毫秒无法保证排序，退化到「仅毫秒分辨率」以下，失去本 change 的核心价值。
- *(b) 传 `timestamp=` 以便对齐外部时钟*：否决，实测破坏单调。

### D3：破坏性范围 = 一次性显式迁移，不保留长期双轨；不提供旧 ID path alias

**决定**：存量 v4 身份经一次性、带备份与可恢复账本、保留 lineage 的显式迁移重编号为 v7；迁移窗口结束后校验器只接受 v7。实现上 MUST NOT 长期 `v4|v7` 并存、MUST NOT 双读、MUST NOT 扫盘重建、MUST NOT 提供旧 ID path alias。

**理由**：

- 与本仓既有纪律一致（AGENTS.md「彻底根除双轨」；`migrate-session-context-uri-to-vrn` 的「入口破坏性拒绝 + 新写字段」「不双读、不留别名、不扫盘重建」；`add-multi-workspace-backend-mounting` D7「新写字段 + 读路径切换」；`add-workspace-persistent-resource-management` 第 6 条）。
- `validate_session_id` 当前**直接拒绝非 v4 位 profile**，所以「只对新写入生效」在实现上等价于「必须同时放宽校验到 v4|v7」——这正是真实的双轨风险。裁定为一次性显式迁移，可在迁移完成后把校验器收敛回单一 v7，避免长期双轨。
- 存量迁移可行性的判据以「代码是否声明了必须迁移的落盘形态」为准：代码明确声明 id 是 `.boxteam/sessions/YYYY/MM/DD/{session_id}` 的**目录叶名**（`session_catalog_store.py` 的 locator 校验）与 SQLite **主键**，且 id 会内嵌进 rollout/message_stream/trace/llm_request 等持久文件（实测同一 session 目录内 10+ 个文件命中该 `ses_` 值），因此存量数据**确实需要迁移**；迁移面广，必须在受维护窗口与一致性备份下用账本推进，而不是「只对新写入生效」糊过去。

**备选**：

- *(a) 只对新写入生效（双版本可接受期）*：否决，等价于长期 `v4|v7` 双轨，违反本仓纪律。
- *(b) 显式失效旧数据*：否决，会销毁用户会话/历史，代价不可接受。

### D4：日期桶 = 必须与 id 内嵌时间戳（UTC）一致，且可校验

**决定**：(a) `sessions/YYYY/MM/DD/{session_id}` 的日期 MUST 与 id 内嵌 48 bit 毫秒时间戳按 UTC 推导的日期一致，不一致即 fail-closed；(b) 时区取 **UTC**（与既有 `created_at.astimezone(UTC).date()` 一致，不改为本地时区）；(c) 时钟回拨时以进程内非递减钳制保证不产出更小 id。

**理由**：

- 现有分桶已用 UTC（`session_catalog_store.py`、`session_control_store.py` 均 `astimezone(UTC)`），改成本地时区会破坏既有 locator 与跨机一致性。
- 让分桶可由 id 复核，才能兑现「方便按时间分桶」的价值主张，并把「分桶与 id 漂移」变成可机械检查的完整性错误。
- 时钟回拨必须显式规定，否则「id 自带时间」会在 NTP 校时下产生乱序；钳制是本地场景下最简单且不误报的做法。

**备选**：

- *(a) 分桶继续独立记账、只把 v7 当额外信息*：否决，等于不兑现价值主张，也留下两套时间事实。
- *(b) 用本地时区分桶*：否决，破坏既有 UTC locator 与跨机一致性。

### D5：校验层正名

**决定**：把 `_validate_uuid_v4_payload` 正名为 `_validate_uuid_payload`，`_UUID_VERSION_HEX_INDEX` 语义变为「要求 version == 7」（名称保持不变、注释/docstring 明确 v7），`_UUID_VARIANT_HEX_CHARS` 不变；`validate_session_id`/`validate_thread_id` 的 docstring 改为「UUIDv7 位 profile」。`app/protocol/canonical.py` 的注释同步。只保留**一个**校验函数，无 v4/v7 分支。

**理由**：本仓严禁同概念异名与双轨；把 `v4` 留在名字里会误导为「当前仍接受 v4」。

**备选**：新增 `_validate_uuid_v7_payload` 与旧函数并存：否决，直接制造双轨。

### D6：SQLite 主键与索引不重建

**决定**：不因 v7 重建表或索引，不加列、不加索引、不加迁移 DDL。

**理由**：v7 与 v4 的文本长度、类型与 `TEXT` 比较语义完全相同，主键定义不变；改变的是值的分布（新写入获得时间局部性）。`nodes.node_id`、`thread_catalog.thread_id` 等既有 `TEXT PRIMARY KEY` 无需任何变更。收益是**新数据**获得 B-tree 插入局部性与按 id 排序≈按时间排序；历史行在一次性迁移重编号后同样受益。

**备选**：重建表以「整理」页空间：否决，收益不抵风险，且违反「零回归/务实」原则。

### D7：JS 服务进程与浏览器前端的 v7 边界

**决定**：`src/workspace-services/{browser,terminal}/server/` 与 `src/clients/web` **不**纳入 v7 强制面；其生成的 id（`term_`/`browser_`/`screenshot_`/`download_`/`page_`/`preset_`/`inline:`）显式声明为**非 canonical 身份**，允许继续使用 v4。

**理由**：

- 实测这些服务由 **Node** 启动（`BOXTEAM_NODE_BIN`，`app/gateway/runtime/process.py`），Node 22 无 `randomUUIDv7`；`src/clients/web` 是浏览器产物且 `src/clients/**` 无任何 `Bun.` 引用——`Bun.randomUUIDv7` 在浏览器不可用。
- 这些 id 并非 session/thread 身份，本就不进入 canonical 校验器命名空间，故继续 v4 不构成双轨。
- MUST NOT 假装它们已统一；若未来 Node 或浏览器提供原生 v7，可作为独立后续变更再收敛。

**备选**：为浏览器打包一个 v7 polyfill：否决，会引入额外前端依赖与体积，且这些 id 非 canonical，收益不足。

### D8：与在途 change 的边界与引用（唯一 owner 声明）

**决定**：本 change 是 id **生成位 profile** 的唯一 owner。具名引用：

- `add-unified-virtual-resource-addressing`：VRN 语法、scope 闭集、`kind` 闭集、拒绝码、ResourceIdentity 定义归它。
- `migrate-session-context-uri-to-vrn`：会话上下文寻址归它。
- `add-multi-workspace-backend-mounting`：workspace 身份与 `scope_id` 推导归它。
- `add-workspace-persistent-resource-management`：资源身份/VRN 持久化归它。

**理由**：VRN/ResourceIdentity 是「不可解析身份」，本 change 改的是「生成位 profile」；两者不可混为一谈，必须显式声明以避免制造第二套定义。

**待 owner 处理的收口项（本 change 不代改）**：`add-itemized-rollout-context` 的 `specs/itemized-rollout-context/spec.md`（写死 `payload` 第 13 个 hex MUST 为 `4`、拒绝「非 v4 bit profile」）、`specs/rollout-checkpoint-storage/spec.md`（同款 v4 断言）与 `add-context-injection-lifecycle` 的 `specs/context-injection-lifecycle/spec.md`、`tasks.md`（同款 `4` 与「非 v4 bit profile」文本）与本 change 冲突，MUST 由其 owner 收口为 v7（列为待处理项，见「待确认问题」）。
**待 owner 处理的收口项（本 change 不代改）**：四处在途文本仍写死 v4，需由各自 owner 收口为本 change 的 v7 定义，具名清单与 owner 声明见 D11。

### D9：哈希/幂等键审计是迁移的阻断前置（owner 裁定）

**决定**：`content_hash` / `plan_hash` / `request_hash` / itemized plan-hash / 幂等键 / 去重键的输入审计 MUST 作为**阻断性任务**排在迁移任务之前；审计结论未落定前迁移 MUST NOT 开工。任何以 id 为输入的哈希/幂等键 MUST 给出明确处置（随迁移一致重算，或该 id 不参与迁移）。

**已实测的现状证据（缩小审计范围，不作为结论）**：

- `app/domain/itemized/hashing.py`：`content_hash(payload_kind, payload)` 输入为 `{payload_kind, payload}`；`contribution_content_hash(contribution_kind, body)` 输入为 `{contribution_kind, body}`。二者**只吃 payload 内容**，不含 session/thread id。
- `app/domain/itemized/hash/plan_hash.py` 的 `context_plan_hash`：hash 投影**显式包含** `session_id`、`plan_id`、`ref_id`、`contribution_id`，故**以 canonical id 为输入**，重编号会使 plan hash 漂移。
- `app/domain/itemized/hash/request_hash.py` 的 `context_request_hash`：包含 `plan_hash()` 与 refs 的 `ref_id`，间接含 id。
- `app/core/session_creation.py` 的 `compute_session_creation_preimage_hash`：四元组为 `{workspace_id, parent_node_id, title, session_metadata}`，**不含 session_id**（session_id 是分配结果，不进 preimage）。
- `app/core/thread_creation.py` 的 `compute_thread_creation_preimage_hash`：含 `session_id`、`thread_id`（thread_id 为非 None 时），故**以 canonical id 为输入**。

因此审计 MUST 覆盖的已知命中面至少包含：`context_plan_hash` / `context_request_hash` / `compute_thread_creation_preimage_hash`，以及全仓 `idempotency_key` 生成点（如 `app/services/infrastructure/rollout_context/storage/transaction.py` 的 `default_*`、`assembly/sealing.py`、`execution/recovery.py`、`reasoning_checkpoint_service.py` 的 `acceptance_*`）。

**为什么裁定为阻断而非「先迁后补」**：迁移后哈希漂移若无人负责，会让 assembly/plan 校验假 mismatch 或静默重算，直接破坏「零回归」与「绝不默默失败」。

### D10：gateway 控制面库逐表分类，失效必须显式报告（owner 裁定）

**决定**：不预先决定控制面是否失效，而是要求**逐表分类**，三类处置语义写死在规范里：`migrate` / `explicitly_invalidated`（MUST 用户可见显式报告）/ `not_affected`（MUST 判定依据）。`user_access_lease` MUST NOT 归入 `explicitly_invalidated`（除非实测证明是「可安全丢弃的租约」），因为租约承担并发互斥，静默丢弃会造成双访问。

**已实测的控制面候选面**：`app/gateway/control/gateway_state.py` 中 `user_view_state` 以 `(user_id, workspace_id, session_id)` 为 `PRIMARY KEY`、`user_access_lease.access_session_id TEXT NOT NULL UNIQUE`。二者承载 session 身份，MUST 进入逐表分类。

**为什么**：对齐「绝不默默失败」/「永不返回虚假的默认值」；租约的安全前提必须由实测支撑。

### D11：在途 change 的 v4 文本收口——本 change 只点名与声明 owner

**决定**：本 change MUST NOT 复制或代改那些文本，只在本节点名四处「仍含 v4 表述、需由各自 owner 收口」的位置，并声明：本 change 是 id 生成位 profile 唯一 owner，上述文本兑现时 MUST 引用本 change，MUST NOT 复述取值。

四处（文件 + capability）：

1. `openspec/changes/add-itemized-rollout-context/specs/itemized-rollout-context/spec.md`，capability `itemized-rollout-context`，requirement「产品 Session、durable Thread 与 LangGraph namespace 必须严格分层」（写死「payload 由 UUIDv4 生成，其第 13 个 hex MUST 为 `4`」「拒绝…非 v4 bit profile」）。
2. `openspec/changes/add-itemized-rollout-context/specs/rollout-checkpoint-storage/spec.md`，capability `rollout-checkpoint-storage`，requirement「rollout storage 必须以 SessionThread 为物理与事务 owner」（写死「payload 第 13 个 hex=`4`…的 UUIDv4 bit profile」「非 v4 bits」）。
3. `openspec/changes/add-context-injection-lifecycle/specs/context-injection-lifecycle/spec.md`，capability `context-injection-lifecycle`，requirements「Context lifecycle owner 必须精确为 SessionThread」与「生命周期场景必须进入统一 Web E2E 验收模块」（写死「payload 第 13 个 hex 为 `4`」「非 UUIDv4 bit profile」「随机 UUIDv4 factory」）。
4. `openspec/changes/add-context-injection-lifecycle/tasks.md`，任务 2.1（写死「payload 第 13 个 hex=`4`」「非 v4 bits」）。

**为什么**：避免第二套定义或重复文本漂移；由 owner 收口可保持单一事实源。

## 风险与权衡

- **迁移面广**：id 内嵌进 rollout JSONL、message_stream、trace、llm_request 等持久文件，且是目录叶名与 SQLite 主键；一次性迁移需要完整账本与备份，中断恢复是主要风险。缓解：复用 `session_catalog_migration.py` 的 staging + journal + 隔离区 fail-closed 先例，禁止扫盘吸收。
- **content_hash / 幂等键**：若某些 hash 或幂等键把 id 作为输入，重编号会改变其值。需在实施时审计（列为待确认项）。
- **依赖提升**：把 `uuid-utils` 从传递依赖变为直接依赖，需确保锁定版本与 cp311–cp314 wheel 覆盖；缺失即 fail-closed，不会静默降级。
- **跨实现单调性差异**：Bun 与 Node 的原生/非原生方案单调性不同，本 capability 只对 Python 唯一工厂给强保证，避免过度承诺。
- **时钟回拨**：钳制是本地进程内保护，跨进程不保证；若未来出现多进程竞争同一毫秒，需要重新评估（当前 id 由后端进程生成，无此面）。

## 迁移计划

1. **准备**：把 `uuid-utils>=0.16` 提升为 `pyproject.toml` 显式直接依赖；把唯一 id 工厂切到 `uuid_utils.uuid7()`；校验器正名为单一 v7 profile。
2. **分桶自校验**：在 locator 校验中加入「`sessions/YYYY/MM/DD` 日期 == id 内嵌 48 bit 时间戳的 UTC 日期」断言（先以校验模式落地，再切换为强制）。
3. **存量迁移**：在维护窗口 + 一致性备份下，运行一次性、可恢复、带 lineage 账本的迁移（session/thread id、目录叶名、SQLite 主键、持久文件内嵌引用），把 v4 重编号为 v7；无法归属者隔离到 `.boxteam/orphaned/`。
4. **收敛**：迁移完成后把校验器收紧为只接受 v7；删除任何过渡读取路径；确认无 v4 生成点残留（除 D7 豁免的非 canonical id）。
5. **验证**：`openspec validate --strict --all`、工厂/校验器单测、迁移中断恢复测试、分桶一致性负向测试。

**阻断顺序（D9/D10 的落点）**：步骤 3 之前 MUST 先完成 D9 的哈希/幂等键阻断审计并落定处置，以及 D10 的控制面逐表分类；审计未落定前步骤 3 MUST NOT 开工。

## 待确认问题

1. **审计任务的具体命中清单**：D9 已给出已实测部分（`context_plan_hash` / `context_request_hash` / `compute_thread_creation_preimage_hash` 命中；`content_hash` / `contribution_content_hash` / session_creation preimage 未命中），但全量 idempotency-key / 去重键清点与负向证据仍需在实施期落成；缺「审计执行结果」才能定最终处置清单。
2. **控制面各表的最终分类**：D10 已定分类语义与 `user_access_lease` 的默认归 `migrate`，但每张表的实际归类（`migrate` / `explicitly_invalidated` / `not_affected`）需实测判定；缺「逐表实测结论」才能定。
