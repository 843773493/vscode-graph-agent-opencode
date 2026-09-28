## Context

现状（实测，`395bb60e`/`e050be42` 系列 HEAD）：

- `app/main.py` 的 `lifespan` 从 `WORKSPACE_ROOT` 环境变量取唯一根，交给 `app/container.py:348` 的 `build_app_container`；该函数在**构造期**把 `workspace_root` 解析为 `resolved_workspace_root`、`resolved_boxteam_root`、`resolved_sessions_root`，并据此装配**全进程唯一**的一份服务图（config/workspace/session/catalog/resource/…）。
- `app/core/path_utils.py` 的 `get_workspace_root()`/`get_boxteam_root()`/`get_sessions_dir()` 直接读环境变量；`get_session_path_resolver()` 与 `get_session_creation_service()` 按会话根目录做 `lru_cache(maxsize=32)` 并让 SQLite catalog 连接**常开进程生命周期**（同文件注释：不提供单独关闭入口）。
- `app/core/sqlite_state.py` 的 `SQLiteProcessOwnership` 按「一个本地状态库一个应用进程」加 flock。
- Gateway 侧 `app/gateway/workspace_ids.py` 生成 `gw_{32hex}` 形式的工作区 ID；`app/gateway/registry.py` 维持 `workspace_id -> service_url` 映射，即「一个工作区一个后端服务地址」。`app/gateway/server/workspace_proxy.py` 已能把 `X-BoxTeam-Workspace-Id` 透传给后端（远端投影分支），但对本地后端没有统一的显式目标传递。
- 工作区**后端**身份是 `app/core/workspace_identity.py` 的 `load_or_create_workspace_id`（严格标准 UUID 文本，拒绝 `gw_` 形式）；这与 Gateway 的 `gw_` 命名空间是**两个不同命名空间**。
- 已提交的 OpenAPI 快照位于 `src/clients/web/openapi.json` 与 `src/clients/web/src/types/openapi/index.json`，由 `scripts/export_openapi_snapshot.py`（`bun run gen:openapi`）生成，并被 `tests/contracts/api/**` 断言与路由一致。

**冻结契约 v2** 由「统一虚拟资源寻址」change 独占，本设计逐字引用其契约术语：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`。本设计只处理 **workspace 身份这一层**，不复述 scope 闭合集、`scope_id` 语义与拒绝码登记（以该 change 的权威表为准）。

**权威表已下发**：scope 闭集、kind 闭集、`builtin`→`inline` 正名与各 scope 的 `scope_id` 取值语义已由「统一虚拟资源寻址」change 的权威表定稿；本设计只引用、不复述这些取值，仅按权威表 R2 落实「`scope_id` MUST 推导自该 scope 的稳定身份、MUST NOT 硬编码」这一原则，并登记 `gateway` 与 `inline`（原 `builtin`）两处既存硬编码违反点。

## Goals / Non-Goals

**Goals:**
- 明确 workspace 身份在 HTTP API 上的显式载体，并给出选型理由。
- 明确 workspace_id 与 VRN `workspace` scope 的 `scope_id` 的身份同一性（不定义语法）。
- 给出必须按 workspace_id 分区的进程级资源清单。
- 给出破坏性迁移、旧字段处置与回滚边界。
- 给出测试影响面的分层口径。

**Non-Goals:**
- 不定义 VRN 语法 / `作用域 / scope` 闭合集 / `scope_id` 语义 / `拒绝码 / rejection code`（引用「统一虚拟资源寻址」change 的权威表）。
- 不改造会话上下文资源的承载（引用「会话上下文 URI 统一改造」change）。
- 不实现跨 gateway 的**星型解析 / star-topology resolution** 转发（引用寻址 change 的解析链）。
- 不落地任何生产代码；本 change 只写规划产物。

## Decisions

### D1: workspace 身份载体 = 路径段为主，代理头为等价内部载体

**决策**：按工作区维度操作的 HTTP API 规范载体为**路径段** `/api/v1/workspaces/{workspace_id}/...`；`X-BoxTeam-Workspace-Id` 请求头为 Gateway 内部代理层的**等价**载体。两者 MUST 指向同一注册表项。

**理由**：
1. **可观测、可缓存、可幂等**：工作区是资源层级的一部分，放进路径使每个工作区资源有唯一 URL，符合 REST 资源定位语义；请求头不参与 URL 身份，会导致「同一资源多种表示」与缓存/重放/日志歧义。
2. **与虚拟资源地址 / VRN 语义对齐（契约 v2）**：`workspace` **作用域 / scope** 里 `scope_id` **必填且等于 workspace_id**，即 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}`（`{gateway_authority?}` 即可选的**网关授权段 / gateway authority**；`{scope}`/`{scope_id}` 取值由「统一虚拟资源寻址」change 的权威表规定；语法本体属该 change，此处仅举例说明与 HTTP 路径段同构）。HTTP 路径段把 `workspace_id` 放在同一层级，前端与后端可在同一「工作区路径前缀」概念下工作，避免「URL 里没有、头里有」的双重寻址。
3. **Gateway 已具头部基础**：现有远端投影分支已在发 `X-BoxTeam-Workspace-Id`（`workspace_proxy.py`），`GatewaySessionContextClient` 也已发该头。保留头作为**代理内部**载体改动最小，但**规范身份**必须是路径段，避免把内部代理约定升格为对外契约。
4. **不选查询参数**：查询参数会被分页/过滤等参数淹没，且不构成资源层级；也不选「请求头为唯一载体」：那会使浏览器直连调试、curl 复现与 URL 级授权都失去工作区维度。

**备选**：仅请求头 / 仅查询参数 / 沿用「当前激活工作区」隐式 —— 前两者如上述缺陷；后者被用户显式否决（「当前工作区」不是寻址概念）。

**破坏边界**：所有不带 `/workspaces/{id}` 前缀、或依赖进程激活态的工作区维度调用都会失效（404/显式拒绝）。

### D2: 为什么必须放弃「当前激活工作区」

「当前激活工作区」是**进程级可变状态**，与多个要求冲突：并发请求无法各自指向不同工作区；持久化记录一旦隐含它，语义就随写入/读取时刻的激活态漂移；跨 gateway（**星型解析 / star-topology resolution**）时激活态无意义。多工作区挂载把这些矛盾从「理论问题」变成「必然故障」，因此 MUST 显式化。契约 v2 把 `scope_id` 对**所有** scope 都设为必填，正是把「workspace_id 必须显式」这条原则扩展到全部 scope，与本决策方向一致。

### D3: 注册表是进程内权威，且与 Gateway 注册表分属两层

**决策**：后端维护「已挂载工作区注册表」（`workspace_id -> 根目录` 等），是**后端侧**权威；Gateway 维持自己的 `workspace_id -> 目标` 路由表。两者通过**同一 `workspace_id` 取值**对齐，但**不共享存储、不互相代理**（Gateway 不直读工作区 `.boxteam/`，符合既有架构原则）。

**理由**：Gateway 控制面数据与工作区业务数据必须物理分离（既有 AGENTS 约束）。注册表只是把「哪个 workspace_id 挂在本进程、根在哪」这条**寻址事实**显式化。

### D4: 路径解析器与 SQLite 连接按 workspace_id 分区

**决策**：`get_session_path_resolver`/`get_session_creation_service` 的 `lru_cache` 键从「会话根目录」改为「workspace_id 或 (workspace_id, 根)」，且每工作区独立 catalog 连接与 `SQLiteProcessOwnership` 锁。`build_app_container` 从「构造期单根」改为「持有注册表 + 按 workspace_id 惰性构造/取用服务图」。

**理由**：长期常开的 SQLite 连接是**每工作区状态**，共享会导致跨工作区事务串扰、锁范围错误、以及 A 工作区的数据被 B 工作区请求读到。惰性构造避免为未使用工作区付出启动代价。

### D5: Gateway 角色 = 仍选目标，但目标显式传递，且与星型解析 / star-topology resolution 一致

**决策**：Gateway 仍负责为请求选择目标工作区，但目标 MUST 经 D1 的显式载体传下去，后端 MUST NOT 猜测。跨 gateway 时，**网关授权段 / gateway authority** 承载**稳定 gateway_id**；拓扑是**星型解析 / star-topology resolution**（hub-spoke，非全互联）：本机是自身联邦 hub 时可直接解析其直接 spoke；本机是 spoke 时经其唯一 hub 做**一次有界 transit**（`visited set` / `max_transit_gateways=1` / `max_gateway_hops=2` / 总 deadline）。解析命中只返回稳定的**资源身份 / ResourceIdentity** 与内容，不携带 locator（locator 是输入不是输出）。不可解析一律 fail-closed 返回结构化**拒绝码 / rejection code**。这些上界 MUST 作为**显式策略常量**，不得散落为硬编码魔法数字。

**理由**：契约 v2 的 D 条已把**星型解析 / star-topology resolution** 细化为 hub-spoke 有界 transit；Gateway 的「选目标」职责与此一致——选出的目标必须以稳定**资源身份 / ResourceIdentity** 显式表达并向下传递，解析结果不得把 locator（含**真实路径 / real path**）反向带回，否则跨边界会泄漏本机路径语义。

**Non-Goal**：本 change 不实现解析链本身（属寻址 change），只声明 Gateway 角色与传递约定同它一致。

### D6: scope_id 推导原则与 gateway/distribution 空洞（与 change 1 对齐的关键补强）

**决策**：本 change 不只在 spec 里声明「`workspace` scope 的 `scope_id` 必填且等于 workspace_id」，而是把它**完整化**为对**所有** scope 的原则：`scope_id` MUST 必填且 MUST **推导自该 scope 的稳定身份**，MUST NOT 硬编码。

- `workspace` → 真实 workspace_id（本 change 的核心，保持）。
- `gateway` → 真实 gateway_id。
- `inline`（原 `builtin`，正名由「统一虚拟资源寻址」change 的 owner 执行，本 change 只引用新名）→ 真实 distribution_id。
- `user` → `local`，显式声明单用户本地约定（该 scope 为本次新增；终值以权威表为准）。

**为什么完整化**：只要求 `workspace` 一处显式，等于允许其它 scope 继续靠硬编码隐含上下文——而「当前激活工作区」这条要根除的原则，其本质是「不得有隐含上下文」。因此把同一原则同构地施加到全部 scope，才是与 change 1 一致的做法（与 D2 同源）。

**两个既存违反点（实测，权威表）**：

| scope | 稳定身份 | 当前实际 scope_id | 违反点 |
|---|---|---|---|
| `gateway` | gateway_id | 硬编码字面量（`app/agents/skill_runtime.py:539` 的 `else` 分支） | 未推导真实 gateway_id |
| `inline`（原 `builtin`） | distribution_id | 同一硬编码字面量（同一 `else` 分支），与 `gateway` **逐字相同** | 未推导真实 distribution_id |

`distribution_id` 更是**零生产赋值**：全仓只在字段定义 `app/services/infrastructure/resource_platform/virtual_resources/values.py:157`、resolver 读取 `resolver.py:205` 与测试中出现，`app/container.py` 无任何装配——这是一个**空洞**。`ResolutionContext` 的唯一构造方也全在测试，生产侧从未构造它、`parse_vrn`/resolver 生产 caller 为零。

**边界**：本 change **不实现**这些取值来源（属「统一虚拟资源寻址」change 或 Gateway 侧），只做两件事：把原则写进 spec，并把空洞登记为本 change 的**接口前提**与风险项。

### D7: 迁移策略 —— 「新写字段 + 读路径切换」，不虚构存量迁移，不留兼容层

**决策**：
- **不虚构存量 VRN 数据迁移**：权威表实测**虚拟资源地址 / VRN 零落盘**（live 安装工作区 157 个 SQLite + dev/temp 44 个库对 `boxteam://` 零命中、无 `resource_activation*` 表；唯一 `display_uri` 列的写入方全在测试、`app/container.py` 无装配、生产 seal 恒为 `None`）。故 workspace 身份显式化**只需新写字段 + 读路径切换**，不得凭空设计 VRN 存量迁移步骤。
- 只描述单工作区前提的**非 VRN** 持久化字段：显式迁移为带 workspace 身份的等价字段，或显式标记失效；不双读、不留别名。
- 环境变量 `WORKSPACE_ROOT` 的定位能力由「注册表 + 显式身份」取代；`get_workspace_root()` 系列不再作为业务解析入口。
- **OpenAPI 快照会因 workspace 身份显式化而失效**：路径形态从 `/api/v1/...` 变为 `/api/v1/workspaces/{workspace_id}/...`，已提交的 `src/clients/web/openapi.json` 与 `src/clients/web/src/types/openapi/index.json` MUST 重新生成（`bun run gen:openapi`），否则 `tests/contracts/api/**` 的快照断言会红。这是**明确的门禁影响**，不是可选优化。

**不可回滚点**：一旦持久化字段按新形态写入并被读取，回到旧形态需再次迁移；迁移脚本本身 MUST 幂等、可在同一形态内重复执行。

### D8: 测试影响面分层口径（因无存量迁移而重述）

因 VRN 零落盘、**无存量迁移**，本形态的实施风险**主要落在接口契约变更**（路由路径形态、显式身份载体、拒绝路径与分区隔离断言），而非数据迁移；据此把测试影响面重述为三层：

- **必须重写**：断言「进程只有一个工作区」、直接读 `WORKSPACE_ROOT`、或复用同一解析器实例跨工作区的用例；API 契约快照类（`tests/contracts/api/**`）——因路径形态变化，需重新生成快照并更新断言。
- **只需加 workspace 参数**：绝大多数 `tests/unit/api/**`、服务层单测——Fixture 已提供独立工作区（如 `tests/unit/core/catalog_workspace_helper.py` 的 `build_catalog_workspace(tmp_path, workspace_id=...)` 已支持显式 `workspace_id`），改为把 `workspace_id` 显式传入请求/装配即可。
- **不受影响**：纯算法/值对象/语法单测（VRN 语法、ID 校验、JSON 序列化等），以及不接触工作区根的纯逻辑测试。
- **新增（scope_id 推导原则的负向断言）**：新增用例断言 `gateway`/`inline` 的 `scope_id` MUST NOT 为硬编码字面量，且 `workspace` scope 的 `scope_id` 与 HTTP 显式寻址的 workspace_id 同源；此属接口契约层断言，不是数据迁移用例。

## Risks / Trade-offs

- [**大量测试改动**（用户已知并接受）] → 用 D8 分层口径分批推进；优先让 fixture 支持显式 `workspace_id`（helper 已具备），把「加参数」与「重写」两类分开，避免一次性巨改。
- [**OpenAPI 快照与前端类型失配**] → 迁移步骤显式包含 `bun run gen:openapi` 与前端类型再生成；门禁在快照未更新时会明确报错（fail-closed），不会静默通过。
- [**路径段与请求头不一致的双载体风险**] → 规定不一致时**显式失败**（spec 已冻结该场景），禁止静默取其一。
- [**两个 workspace_id 命名空间混淆**（后端 UUID vs Gateway `gw_`）] → 本 change 明确：寻址层 `workspace_id` 只使用**后端身份命名空间**（`workspace_identity.validate_workspace_id` 的严格 UUID 文本）；Gateway `gw_` ID 如需保留，只能是 Gateway 控制面内部标识，MUST NOT 进入工作区寻址/VRN。（见 Open Questions）
- [**惰性构造服务图导致首次请求延迟/装配竞态**] → 规定每工作区服务图构造为**进程内幂等**（同一 workspace_id 只构造一次），并沿用既有 SQLite 进程所有权锁语义 fail-closed。
- [**存量 VRN 数据风险已消除**（权威表实测）] → VRN 零落盘（157 live + 44 dev/temp SQLite 对 `boxteam://` 零命中、无 `resource_activation*` 表；`display_uri` 仅测试写入、container 无装配、生产 seal 恒为 `None`），迁移章节因此**不含存量 VRN 迁移**，只需新写字段与读路径切换。
- [**gateway_id 硬编码使跨 gateway 前提未成立**] → `gateway` scope 的 `scope_id` 现为硬编码字面量，而跨 gateway 的**星型解析 / star-topology resolution** 依赖真实 gateway_id；在该值建立真实来源前，跨 gateway 寻址 MUST 视为**未满足的接口前提**（spec 已冻结该场景）。
- [**distribution_id 零赋值空洞**] → `distribution_id` 全仓仅在字段定义、resolver 读取与测试中出现，`app/container.py` 无装配；`inline` scope 的 `scope_id` 推导无真实来源，属待由「统一虚拟资源寻址」change 或 Gateway 侧消除的空洞。
- [**gateway 与 inline 共用同一 scope_id 字面量**] → 二者当前 `scope_id` 逐字相同（`"local"`），与各自稳定身份（gateway_id / distribution_id）不一致；MUST 登记为既有违反点，不得升格为契约形态。

## Migration Plan

**不含数据迁移子步骤**：权威表实测 VRN 零落盘，故本形态**无存量 VRN 数据迁移**；下列步骤均为代码层/接口契约层动作，仅在盘点出「只描述单工作区前提的非 VRN 持久化字段」时才产生一次性字段迁移。

1. 引入注册表与显式身份载体（先不改语义，仅并存读取；但**不**长期保留旧路径）。
2. 把所有工作区根/数据目录定位改为「显式 workspace_id → 注册表」。
3. 把 `build_app_container` 改为「注册表 + 按需服务图」；把 `lru_cache` 键改为 workspace 维度。
4. 按 `scope_id` 推导原则落实身份来源：`workspace` scope 的 `scope_id` 取真实 workspace_id；`gateway`/`inline` 的硬编码字面量 MUST 改为推导自真实 gateway_id / distribution_id（后者来源空洞须先消除，否则 fail-closed，不得继续硬编码）。
5. 处理只描述单工作区前提的**非 VRN** 持久化字段（显式迁移或显式失效）；**无存量 VRN 数据迁移**。
6. Gateway 改为显式传目标（路径前缀或等价头），并与**星型解析 / star-topology resolution** 的 hub-spoke 有界 transit 约定一致。
7. 重新生成 OpenAPI 快照与前端类型；更新契约测试。
8. **回滚**：步骤 1–4 与步骤 6 可在**代码层回滚**（不改变持久化形态，因无存量 VRN 数据）；步骤 5 仅当真的写入了新的非 VRN 字段才产生回滚成本（需再次迁移）；步骤 7 可随时重生成。

## Open Questions

- **权威表已下发，`作用域 / scope` 与 `scope_id` 取值语义已按 R1/R2/R3 定稿**：scope 闭集终值、kind 闭集、`builtin`→`inline` 正名与各 scope 的 `scope_id` 推导来源，均以「统一虚拟资源寻址」change 的权威表为准，本 change 只引用、不复述。仍待定的实现项：(1) `user` scope 的 `scope_id` 终值（现约定为 `local`）；(2) `memory` scope 移出 VRN 后的替代 owner；(3) `gateway_id` 与 `distribution_id` 的真实来源由谁实现。
- 后端身份命名空间（严格 UUID）与 Gateway `gw_` 工作区 ID 是**收敛为一个**还是保留「Gateway 控制面 ID + 后端寻址 UUID」两层映射？本 change 已规定**寻址层只用后端 UUID 命名空间**，此问仅影响 Gateway 控制面是否继续保留 `gw_` 别名，属可延后决定，不改变 spec 的工作区身份定义与任务分解。
- 已挂载工作区的**发现方式**（静态配置 / Gateway 下发 / 启动参数）不影响寻址语义，可延后到实施细节。
