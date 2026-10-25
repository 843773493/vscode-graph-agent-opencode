## Context

原规划基线（实测，`395bb60e`/`e050be42` 系列 HEAD；以下描述不代表当前实现）：

- `app/main.py` 的 `lifespan` 从 `WORKSPACE_ROOT` 环境变量取唯一根，交给 `app/container.py:348` 的 `build_app_container`；该函数在**构造期**把 `workspace_root` 解析为 `resolved_workspace_root`、`resolved_boxteam_root`、`resolved_sessions_root`，并据此装配**全进程唯一**的一份服务图（config/workspace/session/catalog/resource/…）。
- `app/core/path_utils.py` 的 `get_workspace_root()`/`get_boxteam_root()`/`get_sessions_dir()` 直接读环境变量；`get_session_path_resolver()` 与 `get_session_creation_service()` 按会话根目录做 `lru_cache(maxsize=32)` 并让 SQLite catalog 连接**常开进程生命周期**（同文件注释：不提供单独关闭入口）。
- `app/core/sqlite_state.py` 的 `SQLiteProcessOwnership` 按「一个本地状态库一个应用进程」加 flock。
- Gateway 侧 `app/gateway/workspace_ids.py` 生成 `gw_{32hex}` 形式的工作区 ID；`app/gateway/registry.py` 维持 `workspace_id -> service_url` 映射，即「一个工作区一个后端服务地址」。`app/gateway/server/workspace_proxy.py` 已能把 `X-BoxTeam-Workspace-Id` 透传给后端（远端投影分支），但对本地后端没有统一的显式目标传递。
- 工作区**后端**身份是 `app/core/workspace_identity.py` 的 `load_or_create_workspace_id`（严格标准 UUID 文本，拒绝 `gw_` 形式）；这与 Gateway 的 `gw_` 命名空间是**两个不同命名空间**。
- 已提交的 OpenAPI 快照位于 `src/clients/web/openapi.json` 与 `src/clients/web/src/types/openapi/index.json`，由 `scripts/export_openapi_snapshot.py`（`bun run gen:openapi`）生成，并被 `tests/contracts/api/**` 断言与路由一致。

**冻结契约 v2** 由「统一资源寻址」change 独占，本设计逐字引用其契约术语：`资源身份 / ResourceIdentity`、`资源 URI`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`。本设计只处理 **workspace 身份这一层**，不复述 scope 闭合集、`scope_id` 语义与拒绝码登记（以该 change 的权威表为准）。

**权威表已下发**：scope 闭集、kind 闭集、`builtin`→`inline` 正名与各 scope 的 `scope_id` 取值语义已由「统一资源寻址」change 的权威表定稿；本设计只引用、不复述这些取值，仅按权威表 R2 落实「`scope_id` MUST 推导自该 scope 的稳定身份、MUST NOT 硬编码」这一原则，并登记 `gateway` 与 `inline`（原 `builtin`）两处既存硬编码违反点。

## Goals / Non-Goals

**Goals:**
- 实现并验收本 change 的多工作区注册表、显式身份传递、服务图分区与生命周期；已有实现按固定证据复用，只补真实缺口。
- 明确 workspace 身份在 HTTP API 上的显式载体，并给出选型理由。
- 明确 workspace_id 与 URI `workspace` scope 的 `scope_id` 的身份同一性（不定义语法）。
- 给出必须按 workspace_id 分区的进程级资源清单。
- 给出破坏性迁移、旧字段处置与回滚边界。
- 给出测试影响面的分层口径。

**Non-Goals:**
- 不定义 URI 语法 / `作用域 / scope` 闭合集 / `scope_id` 语义 / `拒绝码 / rejection code`（引用「统一资源寻址」change 的权威表）。
- 不改造会话上下文资源的承载（引用「会话上下文 URI 统一改造」change）。
- 不实现跨 gateway 的**星型解析 / star-topology resolution** 转发（引用寻址 change 的解析链）。

## Decisions

### D1: workspace 身份载体 = 路径段为主，代理头为等价内部载体

**决策**：按工作区维度操作的 HTTP API 规范载体为**路径段** `/api/v1/workspaces/{workspace_id}/...`；`X-BoxTeam-Workspace-Id` 请求头为 Gateway 内部代理层的**等价**载体。两者 MUST 指向同一注册表项。

**理由**：
1. **可观测、可缓存、可幂等**：工作区是资源层级的一部分，放进路径使每个工作区资源有唯一 URL，符合 REST 资源定位语义；请求头不参与 URL 身份，会导致「同一资源多种表示」与缓存/重放/日志歧义。
2. **与资源 URI 语义对齐（契约 v2）**：`workspace` **作用域 / scope** 里 `scope_id` **必填且等于 workspace_id**，即 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}`（`{gateway_authority?}` 即可选的**网关授权段 / gateway authority**；`{scope}`/`{scope_id}` 取值由「统一资源寻址」change 的权威表规定；语法本体属该 change，此处仅举例说明与 HTTP 路径段同构）。HTTP 路径段把 `workspace_id` 放在同一层级，前端与后端可在同一「工作区路径前缀」概念下工作，避免「URL 里没有、头里有」的双重寻址。
3. **Gateway 已具头部基础**：现有远端投影分支已在发 `X-BoxTeam-Workspace-Id`（`workspace_proxy.py`），`GatewaySessionContextClient` 也已发该头。保留头作为**代理内部**载体改动最小，但**规范身份**必须是路径段，避免把内部代理约定升格为对外契约。
4. **不选查询参数**：查询参数会被分页/过滤等参数淹没，且不构成资源层级；也不选「请求头为唯一载体」：那会使浏览器直连调试、curl 复现与 URL 级授权都失去工作区维度。

**备选**：仅请求头 / 仅查询参数 / 沿用「当前激活工作区」隐式 —— 前两者如上述缺陷；后者被用户显式否决（「当前工作区」不是寻址概念）。

**破坏边界**：直接 Backend 的工作区请求必须携带路径或等价 header 的显式身份；缺失、冲突或未挂载时明确拒绝。浏览器经 Gateway 的既有同源 `/api/v1/...` 仍可使用，由 Gateway 选择固定目标并注入相同 workspace_id，Backend 不读取激活态补齐。

### D2: 为什么必须放弃「当前激活工作区」

「当前激活工作区」是**进程级可变状态**，与多个要求冲突：并发请求无法各自指向不同工作区；持久化记录一旦隐含它，语义就随写入/读取时刻的激活态漂移；跨 gateway（**星型解析 / star-topology resolution**）时激活态无意义。多工作区挂载把这些矛盾从「理论问题」变成「必然故障」，因此 MUST 显式化。契约 v2 把 `scope_id` 对**所有** scope 都设为必填，正是把「workspace_id 必须显式」这条原则扩展到全部 scope，与本决策方向一致。

### D3: Gateway 唯一持有物理根绑定，后端注册表只是运行时挂载视图

**决策**：负责原生 target 的 Gateway 唯一 registry 按稳定 Gateway target ID 与 Backend `workspace_id` 保存物理 workspace-root binding，并将其作为 Backend 启动输入。Backend 的已挂载工作区 registry 是当前 generation 的运行时 mount view，可在进程内持有实际根供显式 `workspace_id` 查找，但不持久化根、不拥有其来源，也不形成第二份 root registry。Managed Backend group 只记录 target/backend 身份与 generation；Gateway 路由、group membership 与根绑定按稳定身份关联。

**授权设计**：本机变更使用现有本地 managed token，该 token 已代表本 Gateway 的管理权限。远端变更由原生目标 Gateway 最终判权：复用现有 `permissions.federation` / `FederationPolicyStore`，从认证 peer gateway_id 派生 `principal_ref_for_gateway(authenticated_peer.gateway_id)`，要求针对该 principal 的显式 `managed_workspace_admin` allow。授权属于接收请求的目标 Gateway，不按 workspace_id 或路径分配，因此首次创建时不需要已有 workspace_id。pair、read/discovery 等通信权限和 `default_effect="allow"` 都不授予此管理能力；保留其对现有核心通信操作的默认语义。schema、policy 规范化/snapshot/evaluator 与所有注册、替换、移除 callers 在同一批迁齐；不增加 RBAC、权限 registry 或路径 ACL。Relay 只转送，不作最终授权；目标 policy 拒绝时原生 Gateway 返回 403 且不写入 root/group 状态。

**理由**：Gateway registry 是 workspace target 生命周期的控制面权威；Backend 只接收已登记的明确启动输入。现有本地管理 token 和联邦 policy 分别提供本机与远端管理授权，远端用户提供的 target root 只有经原生 owner 授权后才能进入唯一 registry；relay 不复制根。从资源读取权限不能推出注册授权。这样保留外部工作区注册与冷启动，同时避免 Gateway group 或 Backend 进程重启时产生第二份持久化根事实。Gateway 仍不读取工作区 `.boxteam/` 业务数据。

### D4: 路径解析器与 SQLite 连接按 workspace_id 分区

**决策**：`get_session_path_resolver`/`get_session_creation_service` 的 `lru_cache` 键从「会话根目录」改为「workspace_id 或 (workspace_id, 根)」，且每工作区独立 catalog 连接与 `SQLiteProcessOwnership` 锁。`build_app_container` 从「构造期单根」改为「持有注册表 + 按 workspace_id 惰性构造/取用服务图」。

**理由**：长期常开的 SQLite 连接是**每工作区状态**，共享会导致跨工作区事务串扰、锁范围错误、以及 A 工作区的数据被 B 工作区请求读到。惰性构造避免为未使用工作区付出启动代价。

服务图构造期间，已创建的持久 owner 由同一构造作用域逆序关闭；构造成功后才移交原有 `WorkspaceLifespan`。构造失败必须保留原错误并立即释放锁，使同一工作区可以重试，不能依赖垃圾回收。catalog 仍由原 registry 负责，不另建关闭链。

### D5: Gateway 角色 = 仍选目标，但目标显式传递，且与星型解析 / star-topology resolution 一致

**决策**：Gateway 仍负责为请求选择目标工作区，但目标 MUST 经 D1 的显式载体传下去，后端 MUST NOT 猜测。跨 gateway 时，**网关授权段 / gateway authority** 承载**稳定 gateway_id**；拓扑是**星型解析 / star-topology resolution**（hub-spoke，非全互联）：本机是自身联邦 hub 时可直接解析其直接 spoke；本机是 spoke 时经其唯一 hub 做**一次有界 transit**（`visited set` / `max_transit_gateways=1` / `max_gateway_hops=2` / 总 deadline）。解析命中只返回稳定的**资源身份 / ResourceIdentity** 与内容，不携带 locator（locator 是输入不是输出）。不可解析一律 fail-closed 返回结构化**拒绝码 / rejection code**。这些上界 MUST 作为**显式策略常量**，不得散落为硬编码魔法数字。

**理由**：契约 v2 的 D 条已把**星型解析 / star-topology resolution** 细化为 hub-spoke 有界 transit；Gateway 的「选目标」职责与此一致——选出的目标必须以稳定**资源身份 / ResourceIdentity** 显式表达并向下传递，解析结果不得把 locator（含**真实路径 / real path**）反向带回，否则跨边界会泄漏本机路径语义。

**Non-Goal**：本 change 不实现解析链本身（属寻址 change），只声明 Gateway 角色与传递约定同它一致。

### D6: scope_id 身份只引用统一资源寻址的定义

本 change 的寻址合同以 `add-unified-virtual-resource-addressing` requirement「多工作区场景下寻址层必须显式承载 scope_id 身份」为准：本 change 负责显式 HTTP 身份、generation-local mount 与服务图隔离，工作区资源 URI 的 scope_id 直接使用相同 Backend workspace_id，不引入第二套身份或换算层。

scope 闭合集、其它 scope 的身份取值与来源、URI grammar 和拒绝码只引用该 owner 的 requirements「scope 必须取自定稿闭集且 scope_id 对所有 scope 必填」「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」及「拒绝码必须分三套集中登记且命名不得自造」，本 change 不复制取值表。原 gateway/inline 硬编码问题的历史修复提交与验收仍保留在 tasks 1.6，不把历史表当作当前定义，也不重复实施身份来源。

### D7: 迁移策略 —— 「新写字段 + 读路径切换」，不虚构存量迁移，不留兼容层

**决策**：
- **不新增旧开发数据迁移**：原零 URI 取证仅为历史基线；A01 `16f09ee7` 已接通真实 activation freeze/seal/restore，“container 无装配、seal 恒为空”已失效。按用户授权清理旧开发运行数据，workspace 身份显式化只保留新写字段与单一读路径，不增加 URI 扫描、回填或当前来源补造。
- 只描述单工作区前提的旧持久化字段显式失效；清理开发数据前停止对应写入，保留普通源码和独有未集成改动，不双读、不留别名。新的 writer 及恢复错误须修复，不能靠反复清数据掩盖。
- 环境变量 `WORKSPACE_ROOT` 的定位能力由「注册表 + 显式身份」取代；`get_workspace_root()` 系列不再作为业务解析入口。
- **OpenAPI 快照会因 workspace 身份显式化而失效**：路径形态从 `/api/v1/...` 变为 `/api/v1/workspaces/{workspace_id}/...`，已提交的 `src/clients/web/openapi.json` 与 `src/clients/web/src/types/openapi/index.json` MUST 重新生成（`bun run gen:openapi`），否则 `tests/contracts/api/**` 的快照断言会红。这是**明确的门禁影响**，不是可选优化。

**不可回滚点**：新形态已写入后，旧代码不能解释其数据；代码回退若确有需要，应停止 writer 并显式失效、清理开发运行数据，不恢复旧兼容路径。已清理的旧开发数据不承诺恢复；普通源码与未集成改动不在清理范围。

### D8: 测试影响面分层口径（因无存量迁移而重述）

本形态不迁移旧开发数据；验收重点为接口契约（路由路径形态、显式身份载体、拒绝路径与分区隔离）、新写入和精确恢复。测试影响面分为三层：

- **必须重写**：断言「进程只有一个工作区」、直接读 `WORKSPACE_ROOT`、或复用同一解析器实例跨工作区的用例；API 契约快照类（`tests/contracts/api/**`）——因路径形态变化，需重新生成快照并更新断言。
- **只需加 workspace 参数**：绝大多数 `tests/unit/api/**`、服务层单测——Fixture 已提供独立工作区（如 `tests/unit/core/catalog_workspace_helper.py` 的 `build_catalog_workspace(tmp_path, workspace_id=...)` 已支持显式 `workspace_id`），改为把 `workspace_id` 显式传入请求/装配即可。
- **不受影响**：纯算法/值对象/语法单测（URI 语法、ID 校验、JSON 序列化等），以及不接触工作区根的纯逻辑测试。
- **新增（scope_id 推导原则的负向断言）**：新增用例断言 `gateway`/`inline` 的 `scope_id` MUST NOT 为硬编码字面量，且 `workspace` scope 的 `scope_id` 与 HTTP 显式寻址的 workspace_id 同源；此属接口契约层断言，不是数据迁移用例。

### D9: Gateway 共享 Backend 由唯一生命周期 owner 管理

Gateway native root registry 按 target ID 唯一保存物理根，并绑定 Backend UUID；Managed Backend owner 只持有完整成员身份集合、generation 与唯一进程句柄，启动时按 target ID 从该 registry 解析每个成员的根并作为一次性启动输入，不把根复制进 group contract。Backend 的 runtime mount registry 只物化当前 generation 的 mount roots。成员 runtime 只引用 owner，Terminal/Browser 仍归各 target。共享关系显式登记，不按相同 URL 或所有运行 target 推导，也不维护重复的成员或 root authority。普通新增 managed workspace 保持现有独立 singleton 启动行为；只有显式加入共享组才等待整组切换。

唯一生命周期规范为本 change 的 `managed-backend-lifecycle` delta；mounting capability 只引用，不另写排空/force/adopt/close 规则。Backend generation 的 mount 集合不可变；运行中增删成员走现有受控整组 drain→新集合启动→全量身份/健康验证→路由公布，不预造动态 mount RPC。旧组未排空或新组失败时保留/恢复完整旧组，不以一个成员成功代表组成功。

复用现有 Gateway runtime start/stop/safe/force 入口。共享组普通单 target 请求返回 blocked；显式整组请求同时携带 `expected_backend_generation` 与 `expected_workspace_ids`，后者是 Gateway target ID 的完整无重复集合，使用路由 ID 的命名空间，不混成 Backend UUID。owner 锁内重新核 generation/成员，再修改意图或 drain。响应与 runtime 状态提供 `backend_generation` 和完整 `affected_workspace_ids`，blocker 增加归属 `workspace_id`；DTO、OpenAPI、Web runtime hook/确认与必要协议同批迁齐，不把旧单target force 偷偷扩成全组。

safe 在原30秒期限内汇总全组执行与代理引用；超时撤销 drain并保留旧组。force 先持久化全部受影响执行 interrupted，再关闭唯一 Backend handle。remove、替换、shutdown、preserve/adopt、startup recovery 均按 owner 一次处理；故障使全成员 fail-closed/offline并逐成员反馈，按权威完整启动合同仅恢复一次。外部/attached target仍仅探测和透明转发，不因URL相同成为managed成员；默认工作区不可stop的现有限制保留。

共享组配置 restart 只 promotion 发起 target 的匹配 pending proof；sibling 以各自 target ID 从唯一 Gateway registry 重新解析 root binding，并核对 Backend UUID 与健康，不从 group 副本取根。缺少 binding 或 UUID/membership 不符时必须显式拒绝，不能扫描或猜根；失败恢复完整旧 generation 和旧 mount 集合，失败补偿按原 `recovery_required` 合同报告，不新造恢复状态机。当前固定16f只有按target的句柄/锁/恢复；该合同未实施，不把静态计划记为生命周期验收。

实施仍采用整批源码/DTO/调用方/核心公共fixture→少量真实共享进程核心→集中边界回归→最终一次停写冻结。首个核心须经一个Gateway/一个真实managed Backend/两个mount验证身份与数据隔离；边界覆盖单target blocked、完整集合/陈旧generation、全组排空与force、成员进退、一次close/adopt/recovery、配置失败补偿以及外部target边界。

成员变更另有显式 `desired_workspace_ids` 完整目标集合，不从当前集合或path target推导；只接纳已登记managed target，运行中的独立singleton先受控stop，不隐式合并两个活动owner。新组验证前不提交成员权威/运行意图；普通切换若新旧组都不可用，保唯一旧已公布集合、全组offline和候选失败诊断，明确完整组start按最近公布generation/旧启动合同执行恢复。配置恢复仍沿原candidate/journal状态机，不将其recovery_required套给普通成员变更。包含默认target的整组stop和移除默认成员都拒绝，restart仍按合同执行。

desired 未提供表示不变更成员，显式空集合表示已确认全员退出；HTTP、协议与生成绑定保留 presence，不以 repeated 字段默认空值替代确认。发起成员只需属于完整 expected 集合，可以退出 desired 集合；空目标经受控排空后只唯一close并释放 owner，不启动空 mount Backend。

现有 DELETE 保持成功返回完整 WorkspaceList，并委托同一个 owner 成员退出流程。共享组携带同一 GatewayRuntimeGroupRequest，force 使用显式 query 确认；safe blockers 用409 detail返回完整typed lifecycle结果，失败保持target/owner/意图，成功退出与清理后才移除控制面登记。Web按完整影响面确认和暂停/恢复观察流，直接托管与联邦删除同批迁齐；registry.remove不得成为活跃managed Backend的直接close旁路。外部管理/remote target仍沿原控制面归属删除。

停机成员变更使用相同拓扑锁、owner锁与generation/完整集合前置；旧进程/辅助句柄必须已停或清理成功。非空目标经同一次staged/CAS更新唯一owner的generation与完整恢复启动输入，并删除指定target；全affected仍offline/停止意图且无runtime lease，DELETE不启动Backend。safe保留未释放引用时的旧状态，force沿现有引用owner撤销并等待，不向不存在的Backend发业务HTTP。发布前失败保旧generation/成员/登记；发布后观察者错误不回退。后续start全量验证新剩余集合，旧请求陈旧。remote删除先完成可失败SSH转发清理，再移除登记，失败仍可按原target重试。

Gateway自身generation proof/CAS成功前保旧共享owner/handle/完整清单；失败保完整旧组，成功一次接管全组，不逐target adopt。共享配置proof missing/mismatch及retry/resolve/discard均核同一owner generation/完整mount集合；只promotion发起成员的匹配proof，sibling未验证不能公布候选/恢复generation在线，已确认完整旧组按原合同继续服务。

## Risks / Trade-offs

- [**大量测试改动**（用户已知并接受）] → 用 D8 区分必要加参、语义重写和不受影响用例；同一作者一次迁齐完整闭包与公共 fixture，再跑少量真实核心，集中补边界，不逐小包冻结。
- [**OpenAPI 快照与前端类型失配**] → 迁移步骤显式包含 `bun run gen:openapi` 与前端类型再生成；门禁在快照未更新时会明确报错（fail-closed），不会静默通过。
- [**路径段与请求头不一致的双载体风险**] → 规定不一致时**显式失败**（spec 已冻结该场景），禁止静默取其一。
- [**两个 workspace_id 命名空间混淆**（后端 UUID vs Gateway `gw_`）] → 本 change 明确：寻址层 `workspace_id` 只使用**后端身份命名空间**（`workspace_identity.validate_workspace_id` 的严格 UUID 文本）；Gateway `gw_` ID 如需保留，只能是 Gateway 控制面内部标识，MUST NOT 进入工作区寻址/URI。（见 Open Questions）
- [**惰性构造服务图导致首次请求延迟/装配竞态**] → 规定每工作区服务图构造为**进程内幂等**（同一 workspace_id 只构造一次），并沿用既有 SQLite 进程所有权锁语义 fail-closed。
- [**旧取证误当当前生产状态**] → 原零 URI、未装配取证不能覆盖 A01 已接通的 activation；依据固定候选和真实入口收据复用已绿范围，验收当前新 writer 与恢复，不新增旧开发数据迁移。
- [**旧身份装配缺口被误当待实施任务**] → gateway/inline 的装配与硬编码清理已落地，历史提交和验证见 tasks 1.6、3.4-A/B/C；当前身份来源只引用 D6 指向的寻址 owner，不重复派发装配或恢复旧字面量。

## Migration Plan

**不含旧开发数据迁移子步骤**：按用户授权使旧运行数据显式失效并清理；下列代码、接口和新写入步骤保持单一链路，不能将历史零 URI 取证当作当前无持久化 activation 的证明。

1. 引入注册表与显式身份载体，按真实调用链同步迁移全部消费者；每个集成切片只保留一个身份解析入口，不引入新旧并存读取。
2. 把所有工作区根/数据目录定位改为「显式 workspace_id → 注册表」。
3. 把 `build_app_container` 改为「注册表 + 按需服务图」；把 `lru_cache` 键改为 workspace 维度。
4. 按 D6 验证 HTTP workspace_id 与工作区 URI scope_id 同源；其它 scope 沿用寻址 owner 的身份合同及已完成装配，不再重复实施。
5. 使只描述单工作区前提的旧字段显式失效，停止对应写入后清理开发数据；验证新字段和恢复只依赖精确 workspace 身份，不扫描、回填旧 URI。
6. Gateway 改为显式传目标（路径前缀或等价头），并与**星型解析 / star-topology resolution** 的 hub-spoke 有界 transit 约定一致；按 D9 一次迁齐共享 Backend 唯一owner、完整组生命周期和消费者，保普通 singleton 启动语义。
7. 重新生成 OpenAPI 快照与前端类型；更新契约测试。
8. **回滚**：代码与生成快照可以回退或重生成；如数据形态已变化，必须停止 writer 并显式失效、清理开发运行数据，不把新数据交旧 reader，也不增加兼容双轨。已删除开发数据无法由代码回退恢复。

## Open Questions

- scope 闭集、身份来源和现有装配均已定稿，见 D6 的具名 owner 引用及 tasks 的历史收据；本 change 无待裁定的 scope 取值来源项。未来身份演进仍由寻址 owner 负责。
- 后端身份命名空间（严格 UUID）与 Gateway `gw_` 工作区 ID 是**收敛为一个**还是保留「Gateway 控制面 ID + 后端寻址 UUID」两层映射？本 change 已规定**寻址层只用后端 UUID 命名空间**，此问仅影响 Gateway 控制面是否继续保留 `gw_` 别名，属可延后决定，不改变 spec 的工作区身份定义与任务分解。
- 已挂载工作区的**发现方式**（静态配置 / Gateway 下发 / 启动参数）不影响寻址语义，可延后到实施细节。

未注册目录不是本 capability 的 mount 或工作区注册，不得伪造 workspace_id 或增加 WorkspaceTarget。其 URI、唯一原生 Gateway 控制面目录根绑定及路径持久化例外只引用 `add-unified-virtual-resource-addressing` requirement「原生目录根绑定必须由目标 Gateway 唯一持有」，不改变本 capability 的 workspace-root binding 合同。
