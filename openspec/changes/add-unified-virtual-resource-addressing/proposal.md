## Why

当前仓库的 `boxteam://` 地址存在两套并行、互不可解析的解析器：`app/services/infrastructure/resource_platform/virtual_resources/grammar.py` 的 VRN grammar（资源寻址）与 `app/services/business/session_context_resource.py` 的会话上下文正则（会话定位）；且 VRN 的解析/授权侧零生产调用（审计：只有 `skill_display_uri` 一个字符串构造函数被 `app/agents/skill_runtime.py:541` 使用）。skill 链路另有两处绕过 owner 的裸 `boxteam://` 拼接（`skill_runtime.py:52`、`:619`），实测被自家 grammar 以 `malformed_path` 拒绝。 **（修订注，`298ef599`+`f3bd8213`）**：该「零生产调用」已改变——`app/agents/skill_runtime.py` 的 `_layer_scope_identity` 与 `resolver.require_scope_binding` 已构成 `inline`/`workspace` 的生产解析与校验链路。

修订前下发的**契约 v1 语法模板与仓库真实 grammar 不一致**（v1 把 `scope_id` 改成可选、漏掉 `resources` 固定段），本 change 已改用**契约修正 v2**：保留既有段序，并让 `scope_id` 对**所有** scope 都必填——把「workspace_id 必须显式」的原则扩展到所有 scope。

**真实闭集已按仓库内受版本控制的事实源定稿**（取值内联如下，不引用 `out/tests/temp/**` 临时产物）：既有真实 scope 闭集（实测现状）为 `workspace`/`user`/`gateway`/`inline`（`grammar.py:18` 的 `_SCOPE_KEYWORDS` 逐字为该四个取值；`builtin` 正名与 `user` 新增均已由 `298ef599`+`f6fc990f` 落地；`_SKILL_SCOPES` 已不存在、全仓零命中）；`memory` 只在说明符闭集里出现过且已随提交 `32bc6256` 物理移除；既有真实语法 kind 闭集为 `agent-spec`/`skills`（`grammar.py:22` 的 `_RESOURCE_KINDS`），`config` 与 `session` 为本次新增。本 change 据此**定稿**词汇、scope 闭集、scope_id 语义、kind 闭集与拒绝码登记。

用户已拍板统一寻址方向：`workspace` / `user` / `gateway` / `inline` / 其它工作区 / 其它 gateway 收敛到同一套寻址；VRN 解析是顶层 gateway 之间的星型网络；identity 独立于 VRN；配置/skill/状态默认用虚拟地址传递，真实路径只在最后访问点出现。本 change 是这套寻址抽象、词汇、语法、kind 闭集与拒绝码的**唯一 owner**，其余 change 只引用本 change 的定义。

## What Changes

- **BREAKING 统一 VRN 语法（定稿）**：`boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}`。`{gateway_authority?}` 为可选单段、承载稳定 gateway_id；`{scope_id}` 对**所有** scope 必填；`resources` 为固定保留段。现有 `boxteam://workspace/{workspace_id}/resources/skills/{name}/SKILL.md` 是该语法的特例（authority 缺省）。
- **BREAKING scope 闭集（定稿）**：`workspace` | `user` | `gateway` | `inline`。（实测现状闭集已为 `workspace`/`user`/`gateway`/`inline`：`builtin` 正名 `inline` 与 `user` 新增均已落地，`memory` 已移出闭集）。
  - `builtin` MUST 更名为 `inline`，向 config 域既有词汇（`app/schemas/internal_v2/config.py:20` 的 `inline`/`user`/`user_local`/`workspace`/`sqlite`）收敛。
  - **`memory` 已确证不是 VRN scope**：零生产构造方、resolver 连 scope_id 都不比对、`kind="memory"` 全仓零构造、container 未装配（原 `configs/workspace_inline.jsonc:427-433` 的 `agent.memory` 6 键配置块与 schema `$defs.agentMemory` 已随 `remove-agent-memory` 物理删除，现配置已无该块）；其 domain owner 与状态本体从未接入，故无 VRN 替代 owner 的需求。解析器侧 MUST 物理移除既有两点式 `boxteam://memory/{scope}/{name}`（无 `resources`、无 kind、恰好两段）的特例分支，并以 `unknown_scope` 类拒绝码 fail-closed 拒绝（**已由提交 32bc6256 落地**），MUST NOT 留成看似合法的 VRN。
  - **`user` 为本次新增**（权威表证明其尚不存在于 VRN 闭集）。
- **scope_id MUST 由真实身份推导、禁止硬编码**（同时修掉既有不一致；MUST 由唯一一张表规定）：`workspace`→真实 workspace_id；`gateway`→**真实 gateway_id**（现硬编码字面量 `local`）；`inline`→**真实 distribution_id**（`inline` 的 distribution_id 推导已由 `298ef599`+`f3bd8213` 装配落地，不再是零生产赋值；`gateway` 请求级注入已落地（`64ba30c8`/`53befbfc`/`9881a3b2`），不再有 `local` 字面量）；**来源均已定稿**：`inline` 由发行包 runtime manifest 的 `distribution` + `version` 推导（编码规则与缺失 fail-closed 见本 change 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」），`gateway` 由 `${BOXTEAM_HOME}/gateway/identity.json` 的 `load_or_create_gateway_id`（`gateway_<32hex>`）取值、由 Gateway 侧按请求经 `X-BoxTeam-Gateway-Id` 注入（见本 change 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」）；`user`→`local`（显式声明为**单用户本地程序约定**，依据 AGENTS.md 无云服务/无多租户）。**不得自行发明 scope 名或 scope_id 语义。**
- **kind 闭集（定稿）**：`agent-spec` | `skills` | `config` | `session`。`config`（承载配置来源文件本身）与 `session`（承载会话上下文资源/会话定位，由并行 change `migrate-session-context-uri-to-vrn` 消费）为本次新增，均在 kind 闭集登记处定稿。另注：`_RESOURCE_KINDS`（`grammar.py:22`，四值 `agent-spec`/`skills`/`config`/`session`）与 `_DESCRIPTOR_KINDS`（`values.py:28`，两值 `agent-spec`/`skills`）是**两个独立闭集，不可混用**；**描述符闭集当前取值为 `agent-spec`/`skills`，原有成员 `memory` 已随提交 32bc6256 物理移除**（语法侧对两点式 `boxteam://memory/...` 以 `unknown_scope` 拒绝），故不存在「`memory` 只出现在描述符闭集、`ParsedVrn.kind is None`」的错配分支。
- **正名影响评估结论（已独立核验：安全改名，无需存量迁移）**：`layer` 闭集原为 `(bundled, gateway, workspace)`（正名前 `skill_runtime.py:503`，标签表 `:427`）同步正名为 `inline`（现 `layer_order` 为 `(inline, gateway, workspace)`、`_SKILL_LAYER_LABELS` 已移除，原 `bundled`→`builtin` shim 已由 `298ef599` 物理删除）。`layer` 虽进入 `entry_identity`（`:558`）与 catalog payload（`:605`），但二者只进**内存** `ResourceRegistry`（`semantic_registry.py:20-23` 三个 dict，无任何持久化写入）；全仓唯一持久化 `display_uri` 列（`resource_activation_bindings.display_uri`，`resource_activation_schema.py:85`）的写入方 `ResourceActivationStore.persist_snapshot` 生产从不被调用（磁盘取证唯一命中是测试夹具 `tests/unit/core/test_session_control_store.py:2275` 的 `display_uri` 值；该处原为 `boxteam://memory/session/notes`，已随提交 `174b1498` 替换为 `boxteam://workspace/ws-1/resources/skills/notes/SKILL.md`。两者都不改变本结论：该夹具只进测试进程的内存对象，不写任何持久化库；且 `memory` 两点式已随提交 `32bc6256` 被 grammar 物理移除，该形态现被 fail-closed 拒绝）。故改名属**契约级调整而非数据迁移**。**真依赖 VRN scope `builtin` 的位置**现落在 `grammar.py:18` 的 scope 闭集 `_SCOPE_KEYWORDS` 与 `resolver.py` 的 scope 构造校验（grammar.py 已无 `builtin` 字面量、`_SKILL_SCOPES` 已不存在）；其余 `builtin` 命中是无关同名（工具 `origin=builtin`、主题来源、`builtin_tool_registry`），MUST NOT 误改。
- 建立**三层职责分离**并作为核心不变量：`ResourceIdentity`（不透明、稳定、无 revision、不依赖激活工作区、持久化）／`VRN`（可解析、持久化、允许悬空、禁编码 revision/hash）／`real path`（机器本地、临时、永不持久化、永不进模型可见载荷、永不跨 gateway 边界）。
- 细化**星型解析**：`gateway_authority` 承载稳定 gateway_id；本地是 hub 可直接解析直接 spoke；是 spoke 经唯一 hub 做一次有界 transit，携带 `visited set`、`max_transit_gateways=1`、`max_gateway_hops=2` 与总 deadline；上界 MUST 是**显式策略常量**而非散落魔法数字。**解析命中只返回稳定身份与内容，不返回 locator**（`locator 是输入，不是输出`）。不可解析一律 fail-closed。
- **拒绝码集中登记（分三套，不可混用）**：`VrnGrammarError.reason_code` **17 个**（`grammar.py:25-44`，构造函数对未登记 code 直接 `raise ValueError`，闭集不可扩展）、`VrnResolveError` 的规范闭集 **7 个**（现有 6 个加 `unsupported_view`，专用于已识别 view 与资源 kind 不兼容；生产实现仍待任务 1.3 完成）与**联邦解析期第三套**（`app/gateway/federation/errors.py` 的 `FederationError.code`，归属 `app/gateway/federation/`）是**三个独立闭集**，MUST 各列一套、标明各自适用范围与「不可混用」。其它 change 只能引用本登记处。
- **配置来源地址与逻辑来源层由本 change 唯一裁定**：配置文件地址使用既有 `config` kind；VRN 只标识可寻址来源文件，`layer` 与 `precedence` 是独立兄弟字段。具体逻辑 layer 闭集与 precedence 见 requirement「配置 layer 必须表示逻辑来源且与载体和快照分离」；SQLite 只作内部 carrier，`runtime_override` 可无 VRN，`active_snapshot` / `pending_snapshot` 与 `sources[]` 分开。其他 change 只引用该定义，不新增 kind 或 VRN 形态。
- 固化**identity 独立于 VRN**：同一逻辑名出现在两个不同 scope 时是两个不同 identity；跨来源等价/覆盖是独立 concern。
- 固化**默认寻址政策**：配置/skill/状态/资源引用默认用 VRN 传递；新增持久化字段若需定位资源，一律用 `identity + VRN(+ 独立 revision 字段)`，禁止存 real path。
- 建立**可机械检查的 real path 不变量**：real path 出现在 API 响应体／持久化记录／模型可见载荷中即为缺陷，禁止用脱敏或截断静默掩盖。
- **真实路径外泄（已消除，保留为可机械复核的不变量）**：配置来源层记录曾以 `ConfigSourceLayerRecord.source_path`/`backup_path` 把真实路径落进 SQLite，配置来源列表 API 曾以 `ConfigSourceDTO.path`/`ConfigSourcesDTO.schema_path` 把真实路径写进响应体；两处已分别由 `50bffa45`（持久化改 VRN）与 `76ed0089`（响应体改 VRN）消除，详见 requirement「既有配置来源持久化必须按同一模式迁移为 VRN 兄弟字段」。
- **迁移面已确证为「新写字段」而非「存量迁移」**：VRN **零落盘**（157 live + 44 dev/temp 库 0 命中 `boxteam://`）。持久化挂点 = `context_source_control_states`（已存来源事实但无 URI 列）与 `resource_activation_bindings.display_uri`（已建表但生产从不写）。迁移任务一律是「加列 + 写路径」，MUST NOT 写「扫描存量 VRN 实例」。
- **收口四个 change**：在途 `add-context-injection-lifecycle`、并行 `migrate-session-context-uri-to-vrn` 与 `add-multi-workspace-backend-mounting`，以及第四个 `add-workspace-persistent-resource-management`；术语、scope、scope_id 语义、kind 与拒绝码写法一致，无同义异名。
- 命名一致性硬约束：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`；禁止 virtual url / VURI / 虚拟 URL 混用。

## Capabilities

### New Capabilities

- `virtual-resource-addressing`：统一虚拟资源寻址的三层职责分离（ResourceIdentity / VRN / real path）、scope 闭集与必填 scope_id、统一 VRN 语法（保留 `resources` 固定段序）与规范化、kind 闭集（含 `config`/`session`）、可选 gateway authority、星型 gateway 解析链与 policy 常量上界、**分三套集中登记的拒绝码（语法期 17 + 解析授权期 7〔含专用于 view 与资源 kind 不兼容的 `unsupported_view`〕+ 联邦解析期）**、「locator 不是输出」不变量、身份与寻址的职责边界、real path 永不外泄的可检查不变量、`builtin`→`inline` 正名（含 layer），以及配置来源 VRN、逻辑来源 layer、precedence 与快照边界的唯一规范。

### Modified Capabilities

（无。本 change 建立新 capability；`add-context-injection-lifecycle` 的既有 VRN requirement 更新由该 change 自身的 delta 承接，采用「引用本 change」而非在本 change 内改它的 spec。）

## Impact

- 新增 spec：`openspec/specs/virtual-resource-addressing/spec.md`（经本 change 的 delta 建立）。
- 收口修改：`add-context-injection-lifecycle`（接线任务指向本 change；其残留旧词汇与 `memory` 形态已就地收口）、`migrate-session-context-uri-to-vrn`、`add-multi-workspace-backend-mounting`、`add-workspace-persistent-resource-management`（引用本 change 的持久化引用政策）。
- 实施期（本 change 不写生产代码，仅登记影响面）：`app/services/infrastructure/resource_platform/virtual_resources/`（语法/解析/值对象重构：`grammar.py` 的 `_SCOPE_KEYWORDS`/`_RESOURCE_KINDS`（`_SKILL_SCOPES` 已不存在）、`values.py:28` 的描述符闭集）、`app/agents/skill_runtime.py`（两处裸拼接回归 owner；`:538` 的 `bundled`→`builtin` shim 与 `:503` 的 layer 名同步正名，二者均已由 `298ef599` 物理删除，非待实施项）、`app/services/infrastructure/config/state.py` 与 `app/core/config_sources.py`（配置来源 real path → VRN 兄弟字段）、`app/services/business/session_context_resource.py`（由并行 change 负责改造，本 change 只承认其归属），以及 gateway 联邦解析层（星型转发）。
- 与并行 change 的边界：均引用本 change 的术语/scope/scope_id/kind/拒绝码定义，不得各自重新定义；本 change 只负责在寻址层承认「一个后端可挂载多个工作区、scope_id 身份必须显式」。
- **真实路径外泄登记（已消除）**：`app/api/config.py`（`ConfigSourceDTO.path`/`ConfigSourcesDTO.schema_path`）与 `config/state.py`（`ConfigSourceLayerRecord`/`ConfigSourceJournalRecord`）的 real path 输出/持久化已由 `50bffa45`+`76ed0089` 消除（见 What Changes）。
- **待本 change 实施阶段补齐**：`distribution_id` 与 `gateway` 的 `gateway_id` 的**来源均已由本 change 定稿**（前者为发行 manifest 的 `distribution` + `version`，见 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」；后者为 `identity.json` 的 `load_or_create_gateway_id`，由 Gateway 侧按请求经 `X-BoxTeam-Gateway-Id` 注入，见 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」），当前仅**尚未实现装配**（全仓零生产赋值 ≠ 来源未定）；`gateway`/`inline` 的 scope_id 由字面量 `local` 改为真实身份推导后的实测回归。 **（修订注：`distribution_id` 已由 `298ef599`+`f3bd8213` 落地装配；`gateway_id` 请求级注入已落地（`64ba30c8`/`53befbfc`/`9881a3b2`））**
- **前端口径冲突（已登记影响项）**：`src/clients/web/src/state/session/sessionCatalogOutbox.ts:32` 的 `CatalogOutboxPartition.gatewayId` 注释逐字为「稳定 Gateway 身份：本地 Gateway 用其监听端口，远程 Gateway 用其 gateway_id。」，与本 change 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」中「MUST NOT 用监听端口」冲突；统一口径归 Gateway 侧（网关身份由 Gateway 按请求注入、取值由该 requirement 推导），该注释 MUST 在实施期清理。
- 破坏性：现有资源形态与其解析实现、`skill_runtime.py:52` / `:619` 的裸拼接、`bundled`→`builtin` shim 与 layer 名一并收敛，不提供旧形式别名、双读或兼容层。
