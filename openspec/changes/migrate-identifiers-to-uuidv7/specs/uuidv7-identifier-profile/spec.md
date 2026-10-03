## Purpose

为项目所有 canonical 标识符（`session_id`、`thread_id` 及其它由唯一 id 工厂生成的持久身份）规定**唯一**的位 profile：**UUIDv7（RFC 9562）**。本 capability 是 id **生成位 profile** 的唯一 owner：它规定 id 由哪个显式直接依赖生成、如何 fail-closed、同毫秒内的排序保证、`sessions/YYYY/MM/DD` 日期桶如何从 id 内嵌时间戳推导与复核、时钟回拨行为、校验层命名、JS/浏览器侧的可用性边界，以及存量 v4 身份的一次性显式迁移。它**不**定义 VRN、ResourceIdentity、scope 闭集或拒绝码——那些归 `add-unified-virtual-resource-addressing` 等 change，本 capability 只具名引用。

## ADDED Requirements

### Requirement: canonical 标识符的位 profile 必须是 UUIDv7

所有 canonical 标识符（`ses_{32}`、`thr_{32}` 及其它由唯一 id 工厂生成的持久身份）的 32 位 hex payload MUST 满足 **UUIDv7（RFC 9562）** 位 profile：version 位 MUST 为 `7`，variant 位 MUST 属于 `8|9|a|b`；id MUST 保持既有 36-byte ASCII 外形（4 字节前缀 + 32 位小写 hex）。系统 MUST NOT 产出或接受 UUIDv4 位 profile 作为 canonical 身份。

#### Scenario: 工厂产出 v7 位 profile
- **WHEN** 唯一 id 工厂生成一个 canonical 标识符
- **THEN** 其 payload 第 13 个 hex MUST 为 `7`、第 17 个 hex MUST 属于 `8|9|a|b`，且总长 MUST 为 36 个 ASCII byte

#### Scenario: 拒绝非 v7 位 profile
- **WHEN** 任意 API、Proto、catalog 或 typed ref 提交 payload 第 13 个 hex 不为 `7` 的 canonical 标识符
- **THEN** 系统 MUST 在落盘前显式拒绝，MUST NOT 清洗、截断或以 hash 替代该 id

### Requirement: 唯一 id 工厂与其生成来源必须显式且 fail-closed

canonical 标识符 MUST 由唯一 id 工厂（`app/core/identifier.py` 的 `create_uuid_hex` / `create_prefixed_id`）生成；生产代码 MUST NOT 绕过该工厂直接调用底层 uuid 生成 canonical 身份。

生成来源 MUST 是一个**显式直接依赖**：`pyproject.toml` MUST 直接声明 `uuid-utils`（锁定版本 `>=0.16`），MUST NOT 依赖它作为 langchain-core/langsmith 的**传递依赖**偶然存在。依赖缺失、导入失败或 API 不可用时 MUST **fail-closed**（抛出详细错误），MUST NOT 静默回退到 UUIDv4 或任何虚假默认值。

#### Scenario: 直接依赖缺失即失败
- **WHEN** 运行环境未安装 `uuid-utils`（例如依赖被移除）
- **THEN** 工厂在首次生成时 MUST 抛出详细错误并停止，MUST NOT 回退到 `uuid.uuid4()`

#### Scenario: 生产不得绕过唯一工厂
- **WHEN** 生产代码需要生成 canonical session/thread 身份
- **THEN** 它 MUST 经唯一 id 工厂，MUST NOT 自行拼接 uuid 生成逻辑或另建第二个 canonical 生成入口

### Requirement: 同进程内同毫秒的 id 必须非递减且唯一

v7 的时间有序是本 capability 的核心价值，故 MUST 给出可验证的排序保证：

- **同进程**内、**同一毫秒**（相同 48 bit 时间前缀）生成的 canonical id MUST **非递减**（按 32 位 hex 字典序）且 **MUST 唯一**，由 `rand_a` / 计数器方案提供；该保证覆盖真实 Session、main thread 与 child thread allocation 路径，不能只由独立工厂单测代表；
- **跨进程或跨重启**只保证 **48 bit 毫秒分辨率**的时间序；MUST NOT 声称跨进程在同一毫秒内有序；
- 实时 canonical allocation MUST 使用自然 UUIDv7 分配，不得传入显式时间戳参数；显式 `timestamp=` 路径会破坏同毫秒内单调，因此 MUST NOT 用于实时 Session、main thread 或 child thread 创建。为测试注入时间的方式 MUST 驱动自然分配时钟，而不得改走显式时间戳生成路径。

**排序保证的量化上界与对主键局部性的边界（按实测收紧，MUST NOT 弱化）**：同毫秒组实测可达 **3710** 个 id（500000 个样本中 `max ids per same-ms group`），故「同一进程内同毫秒数千个 id 仍保持顺序」是本 capability 承诺的量化上界。据此，`TEXT PRIMARY KEY` 的时间局部性 MUST 表述为「**按毫秒聚簇**的写入局部性」：同进程连续写入获得毫秒内相邻的页插入，而跨进程并发写入同一毫秒时，毫秒内顺序可能交错，局部性退化为毫秒粒度相邻。系统 MUST NOT 声称「主键严格按时间相邻」或「跨进程同毫秒有序」。

#### Scenario: 同毫秒批量生成保持非递减且唯一
- **WHEN** 同一进程在同一毫秒内连续生成 20000 个 id
- **THEN** 按 32 位 hex 排序后与生成顺序逐字节一致，且集合去重后数量等于 20000

#### Scenario: 跨毫秒自然单调
- **WHEN** 同一进程跨越多个毫秒连续生成 200000 个 id
- **THEN** 全局按 32 位 hex 排序后与生成顺序一致，且全部唯一

#### Scenario: 不得声称跨进程同毫秒有序
- **WHEN** 文档或实现描述 v7 的排序保证
- **THEN** 它 MUST 只承诺「同进程内同毫秒非递减且唯一」与「跨进程共享 48 bit 毫秒分辨率」，MUST NOT 承诺跨进程同毫秒有序或主键严格按时间相邻

### Requirement: sessions/YYYY/MM/DD 日期桶必须可由 id 内嵌时间戳推导且一致

新建 Session 的 `created_at` 与 `sessions/YYYY/MM/DD/{session_id}` 日期 MUST 由 `session_id` 内嵌的 48 bit Unix 毫秒时间戳按 **UTC** 推导；系统 MUST 能仅凭 id 复核 locator 日期，MUST NOT 以独立墙钟读取或另存的 `created_at` 作为唯一来源。新建 child Thread 的 `created_at` 与 `threads/YYYY/MM/DD/{thread_id}` 日期 MUST 同样由该 child 自身 `thread_id` 内嵌时间戳按 UTC 推导。main-thread ID MUST 自然分配，但无独立物理日期桶，且不得决定 Session `created_at` 或 Session locator 日期。`thread_catalog.kind=main` 行 MUST 沿用 Session `created_at`；它记录同一 Session 创建事实中的主线程指针，不以 main-thread ID 内嵌时间另定创建时间。上述唯一创建时间源合同仅覆盖 Session 与 child Thread，不扩展到所有 canonical ID 的业务时间字段。

日期 MUST 使用 **UTC**（与既有分桶一致：`session_catalog_store.py` 以 `created_at.astimezone(UTC).date()` 生成 locator）；MUST NOT 改为本地时区。

#### Scenario: 分桶日期与 id 内嵌时间戳一致
- **WHEN** 校验一个 `sessions/YYYY/MM/DD/{session_id}` locator
- **THEN** 从 `session_id` 提取 48 bit 毫秒时间戳并按 UTC 求日期，其结果 MUST 与 locator 的 `YYYY/MM/DD` 完全一致

#### Scenario: 分桶与 id 不一致即 fail-closed
- **WHEN** 某 locator 的 `YYYY/MM/DD` 与 id 内嵌时间戳推导出的 UTC 日期不一致
- **THEN** 系统 MUST 报显式完整性错误，MUST NOT 静默接受、MUST NOT 扫盘修正、MUST NOT 悄悄改桶

### Requirement: 时钟回拨行为必须显式定义为进程内非递减钳制

系统时钟回拨（NTP 校时）时，同一进程内自然分配的 id MUST 保持非递减：MUST 由 UUIDv7 allocation 的单调行为保证，MUST NOT 预读独立墙钟值并以显式时间戳生成 ID，也 MUST NOT 因回拨产出比此前更小的 id。跨进程/跨重启对回拨不做排序保证，但 MUST NOT 产出与既有 id 重复的值。

#### Scenario: 回拨不产生更小 id
- **WHEN** 系统时钟被回拨到早于上一次生成时刻
- **THEN** 新生成的 id 按 hex 序 MUST 不小于上一次生成的 id

#### Scenario: 实时分配的 UUIDv7 时间戳是 Session 与 child 创建时间唯一来源
- **WHEN** 系统通过真实创建路径创建 Session 或 child thread
- **THEN** Session 的 `created_at` 和 `sessions/YYYY/MM/DD` 日期 MUST 从实际分配的 `session_id` 推导；child 的 `created_at` 和 UTC 日期 locator MUST 从该 child 自身 `thread_id` 推导；创建路径 MUST NOT 预读另一墙钟值再传给显式 timestamp helper

#### Scenario: main-thread ID 不改写 Session 时间或目录日期
- **WHEN** Session 创建跨过 UTC 午夜，导致自然分配的 main-thread ID 与 `session_id` 的日期不同
- **THEN** main-thread ID MUST 仍来自自然 UUIDv7 allocation，Session `created_at` 与 `sessions/YYYY/MM/DD` locator MUST 保持由 `session_id` 决定；main row 的 `created_at` MUST 等于 Session `created_at`，main thread MUST NOT 因自身时间戳另建物理日期桶

#### Scenario: 回拨时分桶仍与实际分配 ID 一致
- **WHEN** 系统时钟被回拨后真实创建一个 Session 或 child thread
- **THEN** 自然分配 ID MUST 保持进程内非递减且唯一，实体 `created_at` 与对应 UTC locator 日期 MUST 由该 ID 的内嵌毫秒时间戳推导；一致性校验 MUST NOT 因回拨本身报错

#### Scenario: 不得把回拨变成用户可见故障
- **WHEN** 发生 NTP 回拨
- **THEN** 系统 MUST NOT 默认拒绝创建或报错；若某部署显式选择「拒绝并报告」语义，该选择 MUST 被显式配置，MUST NOT 与默认钳制语义同时生效

### Requirement: 校验层命名必须只反映当前 profile 且不得双轨

把 `v4` 写进名字的标识、注释与 docstring（`_validate_uuid_v4_payload`、`_UUID_VERSION_HEX_INDEX`、「UUIDv4 位 profile」等）MUST 一并正名为与当前单一 profile 一致的名词（例如 `_validate_uuid_payload` / `_UUID_VERSION_HEX_INDEX` 语义变为「要求 version == 7」）。系统 MUST NOT 保留第二套并行校验函数或同概念异名；MUST NOT 长期同时接受 `v4|v7` 两种位 profile。

#### Scenario: 单一校验函数
- **WHEN** 任意入口校验 canonical 身份
- **THEN** 它复用同一个校验函数与同一组 version/variant 常量，MUST NOT 存在按 v4 与 v7 分支的第二套实现

#### Scenario: 命名不再误导
- **WHEN** 阅读校验层标识与 docstring
- **THEN** 其中不出现会误导为「当前仍接受 v4」的名称或描述

### Requirement: 存量 UUIDv4 身份必须一次性显式处置且不得双轨

存量 UUIDv4 身份（`.boxteam/sessions/` 下的 session/thread id、SQLite 主键与相关持久记录）MUST 走**一次性显式处置**：启动/迁移路径遇到 v4 canonical id MUST **fail-closed** 并**隔离**，同时产出**可操作的显式报告**（至少含被隔离 id、物理路径、原因、建议动作）；MUST NOT 回退 v4、MUST NOT 双读、MUST NOT 扫盘重建、MUST NOT 提供旧 ID path alias、MUST NOT 静默吸收。

系统 MUST NOT 提供运行时维护开关（例如 `identity_profile_migration_active`）或任何等价的「窗口期接受 `v4|v7`」运行时分叉；canonical 校验器 MUST **始终只接受 `v7`** 位 profile。

#### Scenario: 遇到 v4 id 时 fail-closed 且报告可见

- **WHEN** 启动/迁移路径遇到 v4 位 profile 的 canonical 身份
- **THEN** 它 MUST fail-closed（隔离到既有隔离惯例，例如 `.boxteam/orphaned/`），并产出包含被隔离 id、物理路径、原因与建议动作的显式报告；MUST NOT 静默吸收、MUST NOT 回退 v4、MUST NOT 猜测目标、MUST NOT 扫盘补洞

#### Scenario: MUST NOT 引入运行时开关

- **WHEN** 检查 canonical 校验器与启动/迁移路径
- **THEN** 其中 MUST NOT 存在 `identity_profile_migration_active` 或任何等价门控开关，MUST NOT 存在「窗口期接受 `v4|v7`」的分支；校验器 MUST 只接受 `v7`

#### Scenario: 处置不双读且不波及 v7 身份

- **WHEN** 存量 v4 身份被处置，或校验一个 v7 身份
- **THEN** 运行路径 MUST NOT 同时按 v4 与 v7 双读同一身份，且 v4 存量处置 MUST NOT 波及合法的 v7 身份

### Requirement: SQLite 主键与索引不因 v7 重建

把 id 生成改为 v7 后，既有以 id 文本作 `TEXT PRIMARY KEY` 的表（`nodes.node_id`、`thread_catalog.thread_id` 等）MUST NOT 因 v7 而重建表或重建索引：v7 与 v4 的文本长度、类型与比较语义完全相同，改变的只是值的分布（获得时间局部性）。系统 MUST NOT 引入新列、新索引或迁移 DDL 来「支持 v7」。

#### Scenario: 无需 DDL 变更
- **WHEN** 实施 v7 切换
- **THEN** 既有主键表结构 MUST 保持不变，MUST NOT 出现仅为 v7 新增的表、列或索引

### Requirement: JS 服务进程与浏览器前端的 v7 可用性边界必须显式声明

系统 MUST 显式声明并区分各运行面的 v7 可用性，MUST NOT 伪造统一：

- `src/workspace-services/browser/server/` 与 `src/workspace-services/terminal/server/` 的后端进程由 **Node** 启动（`BOXTEAM_NODE_BIN`，见 `app/gateway/runtime/process.py`）；实测 Node 22 无 `crypto.randomUUIDv7`。其另行生成的 id（`term_` / `browser_` / `screenshot_` / `download_` / `page_` / `preset_` 等）MUST 被声明为**非 canonical 身份**，允许继续使用 v4。
- `src/clients/web` 是浏览器构建产物，**其生产代码**没有任何 `Bun.*` 引用（实测 `rg -n '\bBun\.' src/clients` 的 8 个命中全部位于 `*.test.ts(x)` 测试文件，生产代码 0 命中；浏览器运行时并不提供 `Bun`）；其生成的附件 file id（`inline:{uuid}:{name}`）MUST 被声明为**非 canonical 身份**，允许继续使用 v4。
- 上述非 canonical id MUST NOT 被当作 session/thread 身份，MUST NOT 进入 canonical 校验器所在的命名空间。

#### Scenario: 非 canonical id 明确豁免
- **WHEN** Node 服务进程或浏览器前端生成一个本地 id
- **THEN** 该 id MUST 被标注为非 canonical 身份，MUST NOT 断言其已满足 v7 profile，也 MUST NOT 让 canonical 校验器接受它作为 session/thread 身份

#### Scenario: 前端不得假装可用 Bun
- **WHEN** `src/clients/web` 需要生成 id
- **THEN** 它 MUST NOT 依赖 `Bun.randomUUIDv7`（浏览器无 `Bun`），并 MUST 按上述非 canonical 边界处理

### Requirement: 与 VRN / ResourceIdentity 的边界必须具名引用

本 capability 只拥有 id 的**生成位 profile**；MUST NOT 定义或改写 VRN 语法、scope 闭集、`scope_id` 语义、`kind` 闭集、拒绝码或 ResourceIdentity。上述内容 MUST 具名引用对应 change，本 capability MUST NOT 复制其定义。

该边界 MUST 有可机械检查的载体（MUST NOT 只作声明）：本 capability 的生成/校验实现（`app/core/identifier.py`、`app/core/session_catalog_store.py` 的 id 校验路径）MUST NOT 导入或引用 VRN/寻址构造（`app/services/infrastructure/resource_platform/virtual_resources/`），且 VRN grammar/解析实现 MUST NOT 承载 UUID version/variant 位判定。

#### Scenario: 生成位 profile 与寻址身份不可混用
- **WHEN** 某处需要引用资源寻址或资源身份
- **THEN** 它 MUST 具名引用 `add-unified-virtual-resource-addressing` / `migrate-session-context-uri-to-vrn` 等 owner，MUST NOT 借本 capability 的 id profile 条款表达寻址语义

#### Scenario: 边界由导入方向机械核对
- **WHEN** 检查 id 生成/校验实现与 VRN 实现的依赖方向
- **THEN** id 侧 MUST NOT 依赖 `resource_platform/virtual_resources/`，且 VRN 侧 MUST NOT 出现 UUID version/variant 位判定；该断言 MUST 由一条静态检查测试承载

### Requirement: canonical 与豁免身份的边界必须显式且豁免不得扩张

系统 MUST 显式枚举哪些 id 属于 **canonical 身份（受 UUIDv7 位 profile 约束）**、哪些属于**明确豁免（允许继续 UUIDv4）**，MUST NOT 只把该区分留在设计散文中。

- **canonical 身份（MUST 满足 v7 位 profile）**：由唯一 id 工厂产出的 `session_id`（`ses_`）、`thread_id`（`thr_`），以及任何进入 canonical 校验器命名空间、被持久化为目录叶名 / SQLite 主键 / typed ref owner key 的 id。
- **明确豁免（MAY 继续 UUIDv4，且 MUST 标注为非 canonical）**：`src/workspace-services/browser/server/` 与 `src/workspace-services/terminal/server/` 在 **Node** 运行时（`BOXTEAM_NODE_BIN`）生成的 `term_` / `browser_` / `screenshot_` / `download_` / `page_` / `preset_` 等本地 id；`src/clients/web` 在**浏览器**（无 `Bun.*`）生成的 `inline:` 附件 file id。

豁免理由 MUST 是可验证的事实：Node 22 无 `crypto.randomUUIDv7`，浏览器无 `Bun.randomUUIDv7`；这些 id 非 session/thread 身份、不进入 canonical 校验器命名空间。

豁免 MUST NOT 被扩张到 canonical id 面：任何 session/thread 身份（含新建的、含由 JS/TS 侧传递进来的）MUST NOT 借本豁免继续使用 v4。

#### Scenario: 豁免枚举可机械核对
- **WHEN** 审计豁免范围
- **THEN** 每个豁免 id 都能对应到一个 Node 服务进程或浏览器生成点，且每个 canonical 身份都能对应到唯一 id 工厂或 canonical 校验器调用点

#### Scenario: 豁免不得扩张到 canonical id
- **WHEN** 某个 session_id 或 thread_id 由 JS/TS 侧（Node 服务进程或浏览器）生成或被提交到 canonical 校验器
- **THEN** 它 MUST 被拒绝，MUST NOT 以「Node/浏览器拿不到 v7」为由复用本豁免

### Requirement: 以 id 为输入的哈希与幂等键必须在迁移前完成阻断性审计并给出处置

存量 v4→v7 重编号会改变任何以 id 为输入的哈希值与幂等键。因此系统 MUST 在**迁移任务开工之前**完成一次**阻断性审计**：逐一判定全仓 `content_hash` / `plan_hash` / `request_hash` / itemized plan-hash / 幂等键 / 去重键的输入是否**直接或间接**包含 `session_id` / `thread_id` / 资源 id（含 `create_prefixed_id` 产物、`display_uri`、`entry_identity`、catalog payload）。

**审计结论未落定前，迁移任务 MUST NOT 开工。** 任何以 id 为输入的哈希或幂等键 MUST 在 change 内给出明确处置：**随迁移一致重算**，或**该 id 不参与迁移**；MUST NOT 留成「迁移后哈希漂移但无人负责」。

审计交付物 MUST 包含：命中清单（文件 + 符号）、每条判定依据，以及「已验证哪些哈希**不**含 id」的**负向证据**（缺负向证据即视为审计不完整）。

已实测的现状证据（不作为结论，仅缩小审计范围）：`app/domain/itemized/hashing.py` 的 `content_hash` 输入只有 `{payload_kind, payload}`、`contribution_content_hash` 只有 `{contribution_kind, body}`；而 `app/domain/itemized/hash/plan_hash.py` 的 `context_plan_hash` 显式含 `session_id` / `plan_id` / `ref_id`。

#### Scenario: 审计未落定则迁移不得开工
- **WHEN** 哈希/幂等键审计的结论尚未落定
- **THEN** 迁移任务 MUST NOT 执行任何重编号，且 MUST NOT 以「先迁后补」推进

#### Scenario: 以 id 为输入的哈希必须给出处置
- **WHEN** 审计发现某哈希或幂等键直接或间接以 id 为输入
- **THEN** change 内 MUST 明确其处置为「随迁移一致重算」或「该 id 不参与迁移」，且 MUST 有对应验证

#### Scenario: 审计必须附负向证据
- **WHEN** 审计报告声称某哈希不含 id
- **THEN** MUST 给出该判定的依据（输入字段清单或可复现的输入注入实验），MUST NOT 只有结论

### Requirement: gateway 控制面库必须逐表分类且失效必须显式报告

gateway 控制面库（`app/gateway/control/gateway_state.py` 等）中承载 session 身份的表 MUST 被逐表分类，分类决定处置，MUST NOT 预先假定可静默失效：

- `migrate`：需要迁移的表，按与工作区数据同一套一次性显式处置语义处理（无运行时开关；失败即 fail-closed 隔离 + 可操作显式报告，不静默吸收）。
- `explicitly_invalidated`：允许失效重建的表，但 MUST 有**用户可见的显式报告**（哪张表、多少行、为何失效）；MUST NOT 静默重建（对齐「绝不默默失败」/「永不返回虚假的默认值」）。
- `not_affected`：判定不受影响者，MUST 给出判定依据。

`user_access_lease` MUST NOT 归入 `explicitly_invalidated`（除非有实测证明其语义是「可安全丢弃的租约」）：租约承担并发互斥，静默丢弃会造成双访问。若需要迁移 MUST 归入 `migrate`；若确需失效 MUST 显式报告并给出互斥安全依据。

#### Scenario: 控制面表被分类为 explicitly_invalidated 时必须显式报告
- **WHEN** 某控制面表被分类为 `explicitly_invalidated`
- **THEN** 系统 MUST 输出用户可见的显式报告（表名、行数、失效原因），MUST NOT 静默重建或返回虚假的成功默认值

#### Scenario: 租约表默认不得被静默丢弃
- **WHEN** 分类处理 `user_access_lease`
- **THEN** 除非有实测的「可安全丢弃」依据，它 MUST 归入 `migrate`，MUST NOT 被静默失效

### Requirement: 迁移面必须按工厂前缀全集枚举且持久面与非持久面分别登记

迁移面 MUST 按唯一 id 工厂（`app/core/identifier.py` 的 `IdentifierPrefix`，实测为 **33 个前缀**的闭合 `Literal`）× **是否进入持久面**的矩阵枚举，MUST NOT 按 `ses_`/`thr_` 等字面后缀匹配。每个前缀 MUST 被判定并登记为持久面或非持久面：

- **持久面**（目录叶名 / 文件名 / SQLite 主键或列 / JSONL 或 JSON 字段）：MUST 列入迁移任务。已实测持久面至少含 `ses`、`thr`、`op`、`strm`、`msg`、`evt`、`snapshot`、`part`、`goal`、`job`、`lease`、`gen`、`grun`、`gwn`、`team`、`ttask`、`tevt`、`comm`、`attempt`、`tooltest`、`patch`、`dbgcfg`、`node-bp`、`node-debug-action`、`node-debug-proc`、`intr`。
- **非持久面**（仅进程内内存/事件总线）：MUST 显式登记为「不参与迁移」并给出负向证据。已实测非持久面为 `req`、`src`、`bgm`、`bgt`、`chan`、`sub`、`robs`。

新增工厂前缀时 MUST 在矩阵补一行并判定持久面，MUST NOT 遗留未判定前缀。

#### Scenario: 矩阵行数等于工厂前缀全集
- **WHEN** 枚举迁移面
- **THEN** 矩阵 MUST 覆盖 `IdentifierPrefix` 的全部前缀（当前 33 个），MUST NOT 只覆盖 `ses_`/`thr_`

#### Scenario: 持久面漏项必须被机械检出
- **WHEN** 某个进入持久面的前缀（例如 `op_` 进 `navigation_mutation_records` 主键、`strm_` 进 `message_streams/*.jsonl` 文件名）未被列入迁移任务
- **THEN** 审计 MUST 报出该漏项，MUST NOT 放行
