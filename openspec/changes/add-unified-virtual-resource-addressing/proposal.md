## Why

当前仓库的 `boxteam://` 地址存在两套并行、互不可解析的解析器：`app/services/infrastructure/resource_platform/virtual_resources/grammar.py` 的 VRN grammar（资源寻址）与 `app/services/business/session_context_resource.py` 的会话上下文正则（会话定位），且 VRN 的解析/授权侧零生产调用（审计结论：只有 `skill_display_uri` 一个字符串构造函数被 `app/agents/skill_runtime.py:541` 使用）。同时 skill 链路内部还残留两处绕过 owner 的裸 `boxteam://` 拼接（`skill_runtime.py:52`、`skill_runtime.py:619`），其实测被自家 grammar 以 `malformed_path` 拒绝。

修订前下发的**契约 v1 语法模板与仓库真实 grammar 不一致**（v1 的简化段序把 `scope_id` 改成可选、且漏掉 `resources` 固定段），本 change 已改用**契约修正 v2**。v2 的关键判断是：简化段序等于对非 workspace 的 scope **重新引入隐含上下文**，而这正是本次改造要根除的东西，因此 v2 保留既有段序，并让 `scope_id` 对**所有** scope 都必填——这是把「workspace_id 必须显式」这条原则扩展到所有 scope，不是「改动更小」的妥协。

用户已拍板统一寻址方向：`workspace` / `user` / `gateway` / `inline` / 其它工作区 / 其它 gateway 收敛到同一套寻址；VRN 解析是顶层 gateway 之间的星型网络；identity 独立于 VRN；配置/skill/状态默认用虚拟地址传递，真实路径只在最后访问点出现。本 change 是这套寻址抽象、词汇与语法定义的**唯一 owner**，另两个并行 change（会话上下文 URI 统一改造、单后端多工作区挂载）只引用本 change 的术语与定义。

**契约状态标注**：本 change 产物的四类内容当前为**初审状态**，其最终权威以 owner 随后下发的**权威表**为准：① scope 名与正名；② scope_id 语义（每个 scope 的取值来源）；③ 拒绝码名称与数量；④ `memory` 的归属与定义。权威表下发前，这四类内容**不得据此实现**。`memory` 作用域**未经本次重新定义**，本 change 不对其做设计。

## What Changes

- **BREAKING** 采用**契约修正 v2** 的统一 VRN 语法：`boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}`。相对 v1 的三处变更：`{gateway_authority?}` 由「任意段」明确为**可选单段、承载稳定 gateway_id**；`{scope_id}` 由「可选、只对 workspace」改为**对所有 scope 都必填**；补回 `resources` **固定保留段**。现有 `boxteam://workspace/{workspace_id}/resources/skills/{name}/SKILL.md` 是该语法的特例（authority 缺省）。
- **BREAKING** scope 闭合集与正名（初审，最终以权威表为准）：`workspace` | `user` | `gateway` | `inline` | `memory`。`builtin` MUST 更名为 `inline`，向 config 域既有词汇（`app/schemas/internal_v2/config.py:20` 的 `inline`/`user`/`user_local`/`workspace`/`sqlite`）收敛，并在落地时**删除** `app/agents/skill_runtime.py:538` 的 `bundled`→`builtin` 改名 shim。`user` 为新增（命名依据同一 config 域前缀）。`memory` 既有存在但**未经本次重新定义**：不基于它做设计、不把它当文件 locator、不为它定义 scope_id。
- scope_id 取值来源（初审，最终以权威表为准，MUST 由**唯一一张表**规定）：`workspace`→workspace_id；`gateway`→`local`；`user`→`local`（已拍板）；`inline`→distribution_id；`memory`→待定（冻结）。**不得自行发明 scope 名、scope_id 语义或拒绝码。**
- 建立**冻结契约 v2** 的三层职责分离并作为核心不变量：`ResourceIdentity`（资源身份，不透明、稳定、无 revision、不依赖当前激活工作区、持久化）／`VRN`（虚拟资源地址，可解析、持久化、允许悬空、禁编码 revision/hash）／`real path`（真实路径，机器本地、临时、永不持久化、永不进模型可见载荷、永不跨 gateway 边界）。
- 细化**星型解析**（权威版）：`gateway_authority` 承载稳定 gateway_id；本地 gateway 是自身联邦 **hub** 时可直接解析其**直接 spoke** 的资源；是 **spoke** 时通过其**唯一 hub** 做**一次有界 transit 解析**，携带 `visited set`、`max_transit_gateways=1`、`max_gateway_hops=2` 与**总 deadline**；这些上界 MUST 是**显式策略常量**而非散落魔法数字。**解析命中只返回稳定身份与内容，不返回、不携带 locator**（`locator 是输入，不是输出`）。不可解析一律 fail-closed。
- **拒绝码集中登记**：复用既有 grammar 拒绝码命名空间与风格，全部拒绝码（含跨 gateway 码）在**唯一一处集中登记处**登记。**具体名称与最少数量以权威表为准**；权威表下发前不得自造或定稿新码名，其它 change 只能引用。
- 固化**identity 独立于 VRN**：同一逻辑名出现在两个不同 scope（例如 `user` 与某个 `workspace`）时是两个不同 identity；跨来源等价/覆盖是另一个 concern，不得塞进 identity 或 VRN。
- 固化**默认寻址政策**：软件内部一切配置/skill/状态/资源引用默认用 VRN 传递；新增持久化字段若需定位资源，一律用 `identity + VRN(+ 独立 revision 字段)`，禁止存 real path。
- 建立**可机械检查的 real path 不变量**：real path 只在调用栈内有效；出现在 API 响应体／持久化记录／模型可见载荷中即为缺陷。
- **登记一条已确证的真实迁移义务**：`app/services/infrastructure/config/state.py:473` 的 `ConfigSourceLayerRecord` 含 `source_path: str` 与 `backup_path: str | None`，即配置来源的 real path **已写入 SQLite**，按「real path 永不持久化」是已存在的违约。改造形态**直接复用 config 侧已跑通的模式**：`app/core/config_sources.py:16` 的 `ConfigSource` 已是 `path` + `layer` + `precedence` 平级属性，且 `layer_revision`/`layer_digest`/`source_generation` 已是兄弟字段 → 即「把 `path: Path` 换成 `vrn: VRN`，其余 sibling 字段原样保留」，**不另发明第二套**。
- **收口在途 change**：更新未归档的 `add-context-injection-lifecycle`，把其未完成的 VRN 接线任务指向本 change，并显式声明「VRN 语法以本 change 为准」，消除两套定义并存的可能。
- 命名一致性作为跨 change 硬约束：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`，禁止 virtual url / VURI / 虚拟 URL 混用。

## Capabilities

### New Capabilities

- `virtual-resource-addressing`: 统一虚拟资源寻址的三层职责分离（ResourceIdentity / VRN / real path）、scope 闭合集与必填 scope_id、统一 VRN 语法（保留 `resources` 固定段序）与规范化、可选 gateway authority、星型 gateway 解析链与 policy 常量上界、拒绝码集中登记、「locator 不是输出」不变量、身份与寻址的职责边界、real path 永不外泄的可检查不变量，以及配置来源持久化的 VRN 兄弟字段迁移。

### Modified Capabilities

（无。本 change 建立新 capability；`add-context-injection-lifecycle` 的既有 VRN requirement 更新由该 change 自身的 delta 承接，采用「引用本 change」而非在本 change 内改它的 spec。）

## Impact

- 新增 spec：`openspec/specs/virtual-resource-addressing/spec.md`（经本 change 的 delta 建立）。
- 收口修改：`openspec/changes/add-context-injection-lifecycle/tasks.md`（接线任务改指向本 change）与其 `specs/context-injection-lifecycle/spec.md`（声明 VRN 语法以本 change 为准）。
- 实施期（本 change 不写生产代码，仅登记影响面）：`app/services/infrastructure/resource_platform/virtual_resources/`（语法/解析/值对象重构）、`app/agents/skill_runtime.py`（两处裸拼接回归 owner；删除 `:538` 的 `builtin` shim）、`app/services/infrastructure/config/state.py` 与 `app/core/config_sources.py`（配置来源 real path → VRN 兄弟字段）、`app/services/business/session_context_resource.py`（由并行 change 负责改造，本 change 只承认其归属）、以及 gateway 联邦解析层（星型转发）。
- 与并行 change 的边界：会话上下文 URI 统一改造、单后端多工作区挂载均引用本 change 的术语/scope/拒绝码定义，不得各自重新定义；本 change 只负责在寻址层承认「一个后端可挂载多个工作区、scope_id 身份必须显式」。
- **待验证项（本 change 不断言，等 owner 查证）**：VRN 是否已落进持久化的 session/catalog/checkpoint 数据（决定迁移是新写字段还是真数据迁移）；`ConfigSource.path` 是否经间接路径进 API 响应体；`inline`/`sqlite` 两层是否有可解析载体；`memory` 的真实形态与归属。以上未确证前不在 spec 中断言。
- 破坏性：现有资源形态与其解析实现、`skill_runtime.py:52` / `:619` 的裸拼接、以及 `bundled`→`builtin` 改名 shim 一并收敛，不提供旧形式别名、双读或兼容层。
