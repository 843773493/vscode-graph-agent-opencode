## 背景

本 change 只改 id 的**生成位 profile**：让 canonical 标识符自带时间序，从而（a）`sessions/YYYY/MM/DD` 分桶可由 id 复核，（b）**同进程内**写入的 B-tree 主键获得时间局部性（量化边界见 D2b）。塑造方案所需的现状与约束如下（均为本轮实测）。

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

### D2b：价值主张的量化边界（按实测收紧，A3）

**决定**：只承诺两件被实测支撑的事，MUST NOT 保留无法用实测支撑的强表述。

- **同进程内**同一毫秒内生成的 id 非递减且唯一：实测 500000 个自然生成 id，同毫秒组最多 **3710** 个（`top5 groups` 约 3699–3710），组内**全部有序、全部唯一**（`non-monotonic same-ms groups: 0`，`global sorted: True`）。这是本 change 提供的**排序保证的量化上界**：毫秒内可达数千个 id 仍保持顺序。
- **跨进程/跨重启**只共享 48 bit 毫秒分辨率：MUST NOT 声称在同一毫秒内跨进程**无排序**。

**对 B-tree 时间局部性的实际影响**（据此替换无法支撑的强表述）：

- 单进程连续写入：同一毫秒内数千个主键落在**相邻页**，局部性最好。
- 跨进程并发写入同一毫秒：插入顺序在毫秒内可能交错，局部性退化为「毫秒粒度相邻」——即按毫秒聚簇、毫秒内可能散开，而不是严格相邻。MUST NOT 把它表述为「主键严格按时间相邻」。
- 因此「B-tree 主键时间局部性」的准确表述是：**按毫秒聚簇的写入局部性，毫秒内仅同进程有序**。

**备选**：

- *(跨进程也保证毫秒内有序)*：否决，需要跨进程共享计数器/锁，本地单机工具不值得引入该复杂度，且无法用本机实测支撑。

### D3：破坏性范围 = 一次性显式迁移 + 同日原子收紧校验器（选方案 a：消除窗口期）

**决定**：**采用「消除窗口期」**（审查给的方案 a）。存量 v4 身份在同一次维护窗口内原子完成「迁移 + 校验器收紧」，窗口期校验器的确切形态判死如下：

- **维护开关**：迁移由唯一的显式开关 `identity_profile_migration_active`（实现期命名可调，语义固定）门控。
  - 开关 **开启**（迁移窗口内）：canonical 校验器接受 `v4|v7`；这是**唯一**允许双接受的时刻。
  - 开关 **关闭**（默认、迁移完成后的常态）：canonical 校验器**只接受 v7**。
- **原子切换**：迁移账本进入终态的那一笔事务提交后，**同一次维护操作内**把开关置为关闭，并把校验器的接受集切到「仅 v7」。MUST NOT 存在「只接受 v4」「长期双接受」的运行态。
- **窗口期不得并行新旧代码**：迁移期间对外服务停止（维护窗口），MUST NOT 让新旧代码版本同时服务同一工作区；实现 MUST 在启动时做版本闸门检查（旧版本进程在开关开启的工作区上 MUST 拒绝启动，或由维护门禁保证单版本）。
- **收敛断言**：开关关闭后，MUST 有一条测试断言「开关为 false 时，`v4` 位 profile 的 canonical 身份被拒绝、`v7` 被接受」；并有一条断言「开关为 true 时，`v4|v7` 均被接受，且该状态 MUST 同时携带显式维护标记」。
- **不要口号**：本决策承认「只要开关处于 true，运行期就存在受控的双接受」；这不是长期双轨，而是**由单一开关门控、有终态、可机械检查的有限窗口**。MUST NOT 用「不双读」这类口号掩盖窗口期——窗口期的双接受由 `identity_profile_migration_active = true` 这一可观察事实显式承认。

**理由**：

- 与本仓既有纪律一致（AGENTS.md「彻底根除双轨」；`migrate-session-context-uri-to-vrn` 的「入口破坏性拒绝 + 新写字段」；`add-multi-workspace-backend-mounting` D7；`add-workspace-persistent-resource-management` 第 6 条）。
- `validate_session_id` 当前**直接拒绝非 v4 位 profile**，所以「只对新写入生效」在实现上等价于「必须放宽校验到 v4|v7」。选方案 a 使双接受成为**有门控、有终态、可断言**的状态，而不是口头承诺。
- 存量迁移可行性的判据以「代码是否声明了必须迁移的落盘形态」为准：代码明确声明 id 是 `.boxteam/sessions/YYYY/MM/DD/{session_id}` 的**目录叶名**（`session_catalog_store.py` 的 locator 校验）与 SQLite **主键**，且 id 会内嵌进 rollout/message_stream/trace/llm_request 等持久文件（实测同一 session 目录内 10+ 个文件命中该 `ses_` 值），因此存量数据**确实需要迁移**。

**备选**：

- *(b) 承认窗口期即双轨并给收口期限*：审查允许，但需要「窗口期长度由什么决定」的额外裁定面，且仍要引入等价的门控开关；方案 a 把同一开关的终态判死为「原子关闭」，约束更强、更少自由度，故选 a。
- *(c) 只对新写入生效（无终态的双接受期）*：否决，等价于长期 `v4|v7` 双轨。
- *(d) 显式失效旧数据*：否决，会销毁用户会话/历史。


### D4：日期桶 = 必须与 id 内嵌时间戳（UTC）一致，且可校验

**决定**：(a) `sessions/YYYY/MM/DD/{session_id}` 的日期 MUST 与 id 内嵌 48 bit 毫秒时间戳按 UTC 推导的日期一致，不一致即 fail-closed；(b) 时区取 **UTC**（与既有 `created_at.astimezone(UTC).date()` 一致，不改为本地时区）；(c) 时钟回拨的确切语义见下方「回拨与分桶互不冲突的判死」。

**回拨与分桶互不冲突的判死（消除审查指出的义务冲突）**：

- 回拨钳制**只作用于新 id 的时间戳来源**：进程维护 `last_issued_ms`，新 id 的毫秒取 `max(monotonic_now_ms, last_issued_ms)`；钳制后的值才是该 id 的真实内嵌时间戳。
- **分桶日期以「创建时刻的已钳制时间戳」为准**：创建流程先用同一个已钳制时间源取得 `effective_created_ms`，再由它同时推出 (i) id 的内嵌时间戳与 (ii) `sessions/YYYY/MM/DD` 的 UTC 日期。因为两者**源自同一个已钳制值**，`id 内嵌时间戳的 UTC 日期 == 分桶日期` 恒成立。
- 因此分桶一致性校验在回拨场景下**不会误伤**：它能检出的是「分桶与 id 人为漂移」，而不是回拨本身。回拨只表现为「新 id 的时间戳停在旧值上」，其分桶仍与之自洽。
- **MUST NOT** 采用「回拨时拒绝创建」作为默认（会把 NTP 校时变成用户可见故障）；若某部署需要更严语义，MAY 选择显式拒绝并报告，但该形态 MUST 与 `S-回拨` Scenario 的可验证断言二选一实现，MUST NOT 两者都写而都不判死。

**原决定（保留）**：(a) 日期 MUST 与 id 内嵌时间戳按 UTC 一致、不一致 fail-closed；(b) 时区取 UTC。

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

- 实测这些服务由 **Node** 启动（`BOXTEAM_NODE_BIN`，`app/gateway/runtime/process.py`），Node 22 无 `randomUUIDv7`；`src/clients/web` 是浏览器产物，其**生产代码**没有任何 `Bun.*` 引用（实测 `rg -n '\bBun\.' src/clients` 的 8 个命中全部是 `*.test.ts(x)` 测试文件（`appErrorBoundary.test.tsx`、`Toolbar.test.tsx`、`themeSurfaces.test.ts` 等），生产代码 0 命中；测试文件因用 `Bun.file`/`Bun.Glob` 等测试 API 而命中，与浏览器运行时无关）——`Bun.randomUUIDv7` 在浏览器不可用。
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

- `app/services/infrastructure/rollout_context/storage/transaction.py` 的 `default_idempotency_key(commit_kind, subject_id, outcome, metadata)`：其签名**显式包含 `subject_id`**，而 `subject_id` 在生产调用点就是 canonical id（如 `assembly/sealing.py` 的 `n=f"assembly:{snapshot.assembly_id}"`、`execution/recovery.py` 的 `resume:{turn_id}:{execution_id}`）。因此该幂等键**直接以 canonical id 为输入**，重编号后必然漂移。

因此审计 MUST 覆盖的已知命中面至少包含：`context_plan_hash` / `context_request_hash` / `compute_thread_creation_preimage_hash` / `default_idempotency_key(subject_id=...)`，以及全仓 `idempotency_key` 生成点（如 `app/services/infrastructure/rollout_context/storage/transaction.py` 的 `default_*`、`assembly/sealing.py`、`execution/recovery.py`、`reasoning_checkpoint_service.py` 的 `acceptance_*`）。

**A5 的处置要求**：`default_idempotency_key` 已判定「会漂移」，故它 MUST 出现在 tasks §5A 的处置表里并给出「随迁移一致重算」或「该 id 不参与迁移」的明确处置，MUST NOT 只登记不处置。

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

### D12：迁移面 = 工厂产出前缀 × 持久面 的全集矩阵（A4）

**决定**：迁移面 MUST 按「**唯一 id 工厂产出的全部前缀** × **是否进入持久面**」的矩阵枚举，MUST NOT 按 `ses_`/`thr_` 字面后缀匹配。`IdentifierPrefix`（`app/core/identifier.py`）实测为 **33 个前缀**的闭合 `Literal`；逐前缀判定见下表。判定口径：进入**目录叶名 / 文件名 / SQLite 主键或列 / JSONL 或 JSON 字段**者记为持久面，只在进程内内存/事件总线中使用者记为非持久面。

| 前缀 | 主要产出点（具名） | 持久面 | 载体证据 |
|---|---|---|---|
| `ses` | `session_catalog_store.py`、`session_catalog_resolver.py`（folder 预留）、`session_navigation/queue_store.py` | **是** | 目录叶名 `sessions/YYYY/MM/DD/{session_id}`（`_STORAGE_LOCATOR_PATTERN`）；`nodes.node_id TEXT PRIMARY KEY` |
| `thr` | `session_catalog_store.py`、`session_control_store.py` | **是** | `thread_catalog.thread_id TEXT PRIMARY KEY`；`threads/YYYY/MM/DD/{thread_id}` 目录叶名 |
| `op` | `session_navigation/service.py`（6 处） | **是** | `navigation_mutation_records` 的 `operation_id TEXT NOT NULL` 且 `PRIMARY KEY (gateway_id, workspace_id, actor, operation_id)`（`queue_store.py`） |
| `strm` | `message_stream_store.py` | **是** | 文件名 `message_streams/{turn_stream_id}.jsonl`（`_stream_path`） |
| `msg` | `message_service.py`、`execution_step/runner.py` | **是** | rollout `messages.message_id TEXT NOT NULL UNIQUE`、`turns.final_message_id`（`rollout_context/storage/schema.py`） |
| `evt` | `job_event_bus.py`、`message_stream_store.py`、`runtime_service.py` | **是** | message_stream 记录字段 `event_id` 写入 `message_streams/*.jsonl` |
| `snapshot` | `message_stream_store.py` | **是** | 作为 `event_id` 写入 message_stream JSONL |
| `part` | `providers/litellm_stream_types.py` | **是** | rollout `item_parts.part_id TEXT NOT NULL`（`schema.py`） |
| `goal` | `session_goal_service.py` | **是** | `SessionCatalogPathResolver` 下的 `goal.json`（`session_goal_store.py`） |
| `job` | `job/service.py`、`session_generation/{reporting,message_dispatch}.py` | **是** | `session_control_store.py` 的 `job_id TEXT NOT NULL UNIQUE` |
| `lease` | `session_control_operation_lease/operation_lease.py` | **是** | `lease_id TEXT PRIMARY KEY`（operation lease 表） |
| `gen` | `gateway/control/generators.py` | **是** | 生成器定义文件路径 `generators/{generator_id}`（`_definition_path`） |
| `grun` | `gateway/control/generators.py` | **是** | 运行记录文件 `generation-runs/{generator_id}/{run_id}`（`_run_path`） |
| `gwn` | `gateway/control/navigation.py` | **是** | Gateway 工作区导航 JSON 持久节点 `node_id`（`atomic_write_json`） |
| `team` | `team/board_manager.py` | **是** | `.boxteam/teams/{team_id}/team.json`（`team/store.py`，且有 `TEAM_ID_PATTERN = ^team_[0-9a-f]{32}$` 形态校验） |
| `ttask` | `team/board_manager.py` | **是** | 团队任务写入 team 事件的 JSON 载荷 |
| `tevt` | `team/board_manager.py` | **是** | 团队事件 JSONL `events.jsonl` 的 `event_id` |
| `comm` | `session_messaging.py` | **是** | `communication_ledger.py` 的 `communication_id TEXT PRIMARY KEY` |
| `attempt` | `tool_testing/service.py` | **是** | `gateway_state.py` 的 `attempt_id TEXT NOT NULL`（及 `last_attempt_id`） |
| `tooltest` | `tool_testing/service.py` | **是** | `tool_testing/store.py` 的 `run.json`（`write_run`） |
| `patch` | `agents/tools/apply_patch/journal.py` | **是** | 文件名 `{journal_id}.json`（`journal_path`） |
| `dbgcfg` | `node_debug/configuration/configuration_registry.py` | **是** | 调试方案 manifest 的 `configuration_id` 写入会话 debug manifest |
| `node-bp` | `node_debug/configuration/configuration_factory.py` | **是** | 断点 `breakpoint_id` 写入调试方案 manifest |
| `node-debug-action` | `node_debug/session/snapshot.py` | **是** | 调试动作 `action_id` 写入 runtime debug 记录 |
| `node-debug-proc` | `node_debug/process/launch_claim.py` | **是** | `process_instance_id` 写入 launch claim durable 记录 |
| `intr` | `session_interrupt_service.py` | **是** | 以自身作 `idempotency_key` / `command_id` 写入受控 command 记录 |
| `req` | `gateway/control/{catalog_search,scheduler,coordinator}.py` | 否（请求内） | 仅作请求/日志关联 id |
| `src` | `background_message_bus.py` | 否（内存） | 仅进程内 bus 消息 `source_id` |
| `bgm` | `background_message_bus.py` | 否（内存） | `self._messages` 内存队列，无落盘 |
| `bgt` | `background_task_registry.py` | 否（内存） | `self._tasks` 内存注册表 |
| `chan` | `events/event_channel_service.py` | 否（内存） | 事件订阅 id |
| `sub` | `job_event_bus.py` | 否（内存） | 订阅句柄 id |
| `robs` | `resource_platform/observation/resource_observation_channel.py` | 否（内存） | 观测订阅 id |

**判定口径说明（防止再次漏项）**：上表以**工厂前缀**为行，而不是以 `ses_`/`thr_` 等字面为行；新增工厂前缀时 MUST 在此矩阵补一行并判定持久面。持久面命中者 MUST 列入迁移任务；非持久面者 MUST 明确登记为「不参与迁移」且在审计里给出「不落盘」的负向证据。

**已实测的核心漏项**：原 tasks 6.1 只匹配裸 `ses_`/`thr_` 字面，会漏掉 `op_`（进 `navigation_mutation_records` 主键）、`strm_`（进 `message_streams/*.jsonl` 文件名）、`msg_`/`evt_`/`part_`/`snapshot_`/`goal_`/`gen_`/`grun_`/`gwn_`/`team_`/`ttask_`/`tevt_`/`comm_`/`attempt_`/`tooltest_`/`patch_`/`dbgcfg`/`node-*`/`intr` 等持久面。

**备选**：

- *只补审查点名的 3 个前缀*：否决，下次新增前缀仍会漏；必须全集枚举。


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
