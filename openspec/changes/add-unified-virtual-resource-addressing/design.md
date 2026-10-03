## Context

现状（全部取自仓库内受版本控制的事实源；关键取值已内联在下方要点中，不引用任何 `out/tests/temp/**` 临时产物）：

- `app/services/infrastructure/resource_platform/virtual_resources/` 定义了严格 VRN grammar 与 typed resolver，但**解析/授权侧零生产调用**：`parse_vrn`、`VirtualResourceResolver`、`ResolvedResourceHandle`、`ResolutionContext` 只被测试引用。 **（修订注，`298ef599`+`f3bd8213`：本 change 3.2/3.3 已装配生产链路，「解析/授权侧零生产调用」已不成立，见下条）**
- 唯一生产使用点是 `app/agents/skill_runtime.py:541` 的 `skill_display_uri`——即大体系只被用来**打印**地址。 **（修订注，`298ef599` 落地）**：`app/agents/skill_runtime.py` 的 `_layer_scope_identity` 已按真实身份推导 `inline` 的 `scope_id`（`f3bd8213` 收紧 version charset），并经 `resolver.require_scope_binding` 校验；「只被用来打印地址」为**修订前现状**。
- `skill_runtime.py:52`（`boxteam://workspace/agents`）与 `skill_runtime.py:619`（`boxteam://workspace/{id}/resources/skills/catalog`）是绕过 owner 的裸拼接，实测被自家 grammar 以 `malformed_path` 拒绝；`boxteam://gateway/...` 等会话定位串同样被拒。
- `app/services/business/session_context_resource.py` 是**第二套并行**的 `boxteam://` 正则解析器（会话定位，支持 `#fragment`），与 VRN grammar 互不可解析。
- 在途未归档 change `add-context-injection-lifecycle` 的 `tasks.md:47`（3.14）解析器本体已完成，`tasks.md:95`（6.6）与 `:111`（7.1）接线任务未做；其 `specs/context-injection-lifecycle/spec.md:331/333` 有 VRN resolver requirement。

**契约版本与权威表状态**：本设计采用**契约修正 v2**（保留既有段序，`resources` 为固定保留段，`scope_id` 对所有 scope 必填），并已依据**已下发的权威表**定稿三类内容：scope 闭集、scope_id 语义、kind 闭集与拒绝码登记。权威表推翻 v2 两处初审：既有 scope 闭集其实只有 `workspace`/`gateway`/`builtin`/`memory`，`user`/`inline` 为新增；kind 既有只有 `agent-spec`/`skills`，`config` 与 `session` 为新增。

## Goals / Non-Goals

**Goals**

- 把「身份 / 地址 / 真实路径」三层分离落成**唯一 owner 与唯一实现**，并给出可机械检查的不变量。
- 给出一套统一 VRN 语法（保留 `resources` 固定段序）与规范化单一实现、**定稿**的 scope 闭集、scope_id 语义表与 kind 闭集。
- 给出**分三套**集中登记的拒绝码（grammar 17 + resolve 7 + 联邦解析期）与其不混用约束；resolve 闭集包含 `unsupported_view`，专用于已识别的 view 与资源 kind 不兼容。
- 给出顶层 gateway 之间**星型解析**的边界契约，并把上界做成显式 policy 常量。
- 登记配置来源 real path 持久化这一**已存在违约**的迁移形态，并唯一规定来源 VRN、逻辑 layer/precedence 与 carrier/snapshot 的边界。
- 明确与另外三个 change 的接口面，避免多份定义并存。

**Non-Goals**

- 不在本 change 内写生产代码；实施由 tasks 驱动。
- 不设计会话上下文 URI 的统一改造（由并行 change 承载），本 change 只承认其归属与 scope/kind 复用。
- 不设计 HTTP API 的多工作区路由细节（由「单后端多工作区挂载」change 承载），本 change 只要求 scope_id 身份在寻址层显式。
- **不为 `memory` 做任何设计**：它已确证不是 VRN scope，且其 domain owner 与状态本体从未接入，故无 VRN 替代 owner 的需求；解析器侧 MUST 物理移除既有两点式特例分支，并以 `unknown_scope` 类拒绝码 fail-closed 拒绝（**已由提交 32bc6256 落地**）。
- 不引入资源插件宿主、动态 provider 装配或可安装资源 API。

## Decisions

### D1：三层分离以「谁持久化 / 谁可跨边界」为切分线

- **ResourceIdentity**：持久化，无 revision，不依赖激活工作区 → 用于去重与 lineage。
- **VRN**：持久化，允许悬空，禁 revision/hash → 软件内部与模型可见载荷的默认引用形式。
- **real path**：不持久化、不进模型可见载荷、不跨 gateway → 只在 fs/sqlite 调用点作为局部变量。

**理由**：审计发现的真实缺陷正是「路径形状」泄漏（skill 目录扫描、`skillPaths.ts` 解析物理路径、catalog URI 裸拼接、`ConfigSourceLayerRecord.source_path` 落库）。以「是否持久化」「是否可跨边界」两条可机械判定的性质切分，比按命名切分更抗漂移。

**备选**：把 revision 编进 VRN（被否：revision 变化会让同一资源产生不同地址，破坏「地址稳定、revision 独立」）；把 real path 作为可选调试字段随记录持久化（被否：正是当前要消除的违约，且违反跨 gateway 边界约束）。

### D2：保留既有段序，scope_id 全必填

采用 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...}`，而非简化段序。

**理由**：简化段序（把 `scope_id` 变可选、去掉 `resources`）会让 `user`/`gateway`/`inline` 这些 scope **重新依赖隐含上下文**来补齐身份，与「根除当前工作区隐含前提」的目标直接矛盾；且会与仓库真实 grammar（既有 `resources/{kind}` 段序）分叉，制造第三套形态。保留段序 + scope_id 全必填，是「沿用真实 grammar 且把显式原则推广到所有 scope」。

**备选**：简化段序（被否，见上）；为每个 scope 定制不同段序（被否：多套语法正是本 change 要消除的）。

### D3：scope 闭集定稿为 workspace/user/gateway/inline，memory 移出

**理由**：权威表实测**当时的**既有闭集只有 `workspace`/`gateway`/`builtin`/`memory`；`user` 是用户要求新增。`memory` 零生产构造方、resolver 不比对 scope_id、`kind="memory"` 零构造、container 未装配，且两点式形态（无 `resources`、无 kind）会绕过固定段与 kind 校验——把它当 scope 等于在统一语法上开后门。故移出并显式标注「`memory` 不是 VRN scope」，且因其 domain owner 与状态本体从未接入（无 VRN 替代 owner 的需求），解析器侧 MUST 物理移除该两点式特例分支并以 `unknown_scope` 类拒绝码 fail-closed 拒绝（**已由提交 32bc6256 落地**：特例分支与 `memory_display_uri` 已删除，`_SCOPE_KEYWORDS`/`_DESCRIPTOR_KINDS` 均不再含 `memory`）。

**备选**：保留 `memory` 于闭集并为其定义 scope_id（被否：无任何事实支撑，会固化一个空洞）；把 `memory` 两点式当合法特例（被否：破坏「单一语法」，即本次要根除的双轨）。

### D4：scope_id 必须由真实身份推导，禁止硬编码

**理由**：现状 `skill_runtime.py:539` 把 `gateway` 与 `builtin` 的 scope_id 都硬编码为 `local`，二者逐字相同；resolver 期望的 `distribution_id` 全仓零生产赋值。让 `gateway`→真实 gateway_id、`inline`→真实 distribution_id，才能让 scope_id 真正承担身份，否则「必填」只是形式。`user`→`local` 依据 AGENTS.md 的单用户本地程序前提。`inline` 的来源已由 D4b 定稿为发行 manifest 的 `distribution` + `version`。 **（修订注，`298ef599` 落地）**：该 `else "local"` 已按 3.3 拆开，`inline` 改走 `app/core/distribution_identity.py` 的 `load_distribution_id`（`f3bd8213` 把 version charset 收紧为 `[A-Za-z0-9.-]`），`distribution_id` 不再是全仓零生产赋值。

**备选**：继续用 `local` 字面量（被否：scope_id 失去区分能力，且 `inline` 与 `gateway` 冲突）。

### D4b：inline 的 distribution_id 来源与编码定稿（用户裁定）

**决定**：`inline` scope 的 `distribution_id` 由发行包 runtime manifest（`packages/launcher/runtime-manifest.schema.json`）的 `distribution` + `version` 确定性推导；`version` 合法字符集为 `[A-Za-z0-9.-]`（semver 标识字符集，**不含 `_`**），编码规则为 `.`→`_` 的**单射**映射，再以 `-` 连接 `distribution`。**唯一实现**是 `app/core/distribution_identity.py` 的 `_VERSION_PATTERN` 与 `encode_version`（`version.replace(".", "_")`），已由 `f3bd8213` 落地，本节口径 MUST 与该实现保持一致。

**实测依据**：manifest 实测路径 `out/development-runtime/runtime-manifest.json`（由 `scripts/launch/dev.mjs:177-207` 写、`packages/launcher/src/gateway-supervisor.mjs:90` 以 `BOXTEAM_RUNTIME_MANIFEST` 传给 Python 侧），真实字段 `distribution: "source-development"`、`version: "0.0.2"`；`version` 取自根 `package.json`（`packaging/runtime/versions.mjs` 的 `BOXTEAM_VERSION`）。`distribution` 是 schema 枚举 `source-development`/`source-installed`/`npm`/`standalone`，全部落在 VRN charset `[A-Za-z0-9_-]` 内。

**风险与真实反驳**：`version` 实测含点号（`0.0.2`），而 `_NAME_CHARSET`（`grammar.py:23`）不含 `.`。用真实 `parse_vrn` 实测，`boxteam://workspace/source-development-0.0.2/resources/skills/x/SKILL.md` 被 `invalid_character` 拒绝 —— 直接拼接不可行。改用 `.`→`_` 编码后 `source-development-0_0_2` 解析通过。该映射是**单射**（`_VERSION_PATTERN` 已排除 `_`，`_` 只可能来自原点号），但**不是可逆/解码**关系：生产 resolver 只做 `scope_id` 字符串比对（`resolver.py:139`），不提供解码函数。早先「先 `_`→`__`、再 `.`→`_`」的双步转义**不是单射**：`_` 本身就在其旧 charset `[A-Za-z0-9._-]` 内，与原点号同码，单符 `_` 与 `..` 会得到同一个编码 `__`。该双步转义已由 `f3bd8213` 删除，MUST NOT 恢复。

**为什么不用「去点法」**：`version.replace(".", "")` 会把 `0.0.2`/`0.02`/`00.2` 全部压成 `002`（实测碰撞、非单射），故否决。

**拆分唯一性**：`distribution` 枚举自身含 `-`（如 `source-development`），故还原 MUST 用枚举最长前缀匹配，MUST NOT 用「首个 `-`」裸切；枚举内无一取值是「另一取值 + `-`」的前缀，故 `(distribution, version)` 拆分唯一。`version` 含 `[A-Za-z0-9.-]` 之外的字符（含 `_`、`+`）时 fail-closed，MUST NOT 静默丢弃。

**为什么 MUST NOT 放宽 charset**：charset 与拒绝码是已登记 grammar 的一部分；为解决一个编码问题而放宽 `.`，会引入 `.`/`..` 段混淆与规范化歧义，并与「不新增转义后门」冲突。故选编码、不放宽语法。

**为什么 MUST NOT 用目录名/安装路径**：同一发行包在不同机器、不同路径安装时路径必不同；用它会让跨 gateway 寻址从根上不成立。manifest 的 `distribution`+`version` 是发行包自带的稳定身份。

**缺失 fail-closed**：`distribution`/`version` 缺失即显式失败，不设 `local` 默认（AGENTS.md「永不返回虚假的默认值」）；开发态 `source-development` 亦走同一 manifest，不单开分支。

**备选**：放宽 charset 允许 `.`（被否，见上）；用目录名或环境变量（被否：不稳定）；缺字段时回退 `local`（被否：虚假默认值）。

### D4c：gateway 的 gateway_id 来源与注入 owner 定稿（用户裁定）

**决定**：`gateway` scope 的 `scope_id` 取值来源为 `${BOXTEAM_HOME}/gateway/identity.json` 中由 `app/gateway/credentials.py:138` 的 `load_or_create_gateway_id` 生成的随机不透明 id（形如 `gateway_<32hex>`）；MUST NOT 用 host:port 或监听端口。注入 owner 为 **Gateway 侧**：Gateway 代理 `/api/v1/*` 时附加 Gateway 身份头（按既有 `X-BoxTeam-*` 约定命名为 `X-BoxTeam-Gateway-Id`），workspace 后端从请求上下文读入。因同一 workspace 后端可被不同 Gateway 挂载，gateway 身份 MUST 按请求注入并读取，MUST NOT 用进程级单例或「当前激活」态；缺失或非法时 fail-closed。MUST NOT 改写或复用 `X-Request-ID` 的语义与职责。

**实测依据**：`load_or_create_gateway_id` 生成 `f"gateway_{secrets.token_hex(16)}"` 并写入 `identity.json`；`app/gateway/` 与 `app/gateway/remote_gateway.py` 已在该路径读写（原 `main.py` 行号随 `45f9ada2` 拆分失效：现分布于 `lifespan.py`/`routes/**`）。仓库既有 `X-BoxTeam-*` 头为 `X-BoxTeam-Federation-Token`/`X-BoxTeam-Workspace-Id`（`app/gateway/auxiliary_proxy.py:91-92`、`app/gateway/registry.py:1837-1838`），此前无 gateway 身份头。

**前端口径冲突（登记影响项）**：`src/clients/web/src/state/session/sessionCatalogOutbox.ts:32` 的 `CatalogOutboxPartition.gatewayId` 注释逐字为「稳定 Gateway 身份：本地 Gateway 用其监听端口，远程 Gateway 用其 gateway_id。」，与本决定「MUST NOT 用监听端口」冲突；统一口径归 Gateway 侧（网关身份由 Gateway 按请求注入、取值按 spec requirement 推导），该注释 MUST 在实施期清理。

**备选**：用 host:port 或监听端口（被否：不稳定且跨机不可比）；复用 `X-Request-ID` 承载（被否：违反「任何一层不得补造第二个请求 ID」）。

### D5：authority 缺省 == 本机 gateway == 显式本机 gateway_id

**理由**：既有形态是「authority 缺省」的特例，大量在途规划依赖「不带 authority 即本机」。让显式本机 gateway_id 与缺省**同解**，可避免「本机 vs 显式本机」被当成两个地址而破坏规范化。

**备选**：二者含义不同（被否：产生两个等价地址，持久化记录因写法不同失去可比性）。

### D6：星型解析在 gateway 层收敛，边界只传逻辑事实，上界是 policy 常量

链路：本地 parse（fail-closed）→ 本机按 workspace registry 解析 / 对端按联邦关系转发（hub 直连直接 spoke；spoke 经唯一 hub 一次有界 transit）→ 不可达/未共享/未找到一律 fail-closed。

**理由**：只有实际持有资源的进程能做权威解析；转发方不得猜测路径。把 `visited set`/`max_transit_gateways`/`max_gateway_hops`/总 deadline 做成显式 policy 常量，是因为拓扑会变，解析器不应因拓扑调整而重写。星型（最多一次中继）与仓库既有联邦拓扑一致。

**不变量**：`locator 是输入，不是输出`。

**备选**：转发方缓存并本地解析对端资源（被否：第二份事实源）；对端返回 real path 由本机访问（被否：跨边界传路径，且远端路径在本机无意义）；把上界写死在解析器里（被否：拓扑变化即改代码）。

### D7：拒绝码分三套集中登记，不可混用

**理由与裁定**：规范包含三个独立拒绝码闭集——`VrnGrammarError.reason_code` 17 个、resolve 解析/授权期闭集 7 个（原有 6 个加 `unsupported_view`）与**联邦解析期第三套**（`app/gateway/federation/errors.py` 的 `FederationError.code`，归属 `app/gateway/federation/`）。`unsupported_view` 专用于已识别的 view 与资源 kind 不兼容。当前生产 resolver 仍只登记原有 6 个 resolve 码；任务 1.3 保持未完成，须在实现中加入 `unsupported_view` 并维持闭集构造校验。三套分别对应「字符串→ParsedVrn 的语法期」「已解析后的解析/授权期」与「跨 gateway 联邦解析期」，混用会让阶段职责错位。集中登记可机械检查「无自造同义码」。

**备选**：合并为一套码（被否：语义阶段不同，合并会丢掉「哪一期拒绝」的信息）；每个模块自带枚举（被否：正是要消除的多份定义）。

### D8：identity 与 VRN 的职责边界以「可解析性」划分

identity 不可解析、不做寻址；VRN 可解析、不承担身份。同一逻辑名跨 scope 是两个 identity；跨来源覆盖是独立 concern。

**理由**：审计里 `SemanticResourceDescriptor` 双类同名与 `contracts.py` 的 TODO 表明，之前把「身份」与「地址/owner scope」耦合，导致字段语义漂移。以「可解析性」切分最清晰。

**备选**：让 VRN 兼任 identity 用于去重（被否：VRN 允许悬空，悬空后无法去重）。

### D8b：kind 闭集为 config 与 session 各登记一次（owner 裁定，消除待登记）

**决定**：kind 闭集定稿为 `agent-spec` | `skills` | `config` | `session`。会话上下文资源（会话定位）**自身**的 kind 取 `session`，由本 change 在此登记；`config` 承载配置来源文件本身。两者都不再由别的 change 新登记。

**理由**：闭集一旦闭合，未登记 kind 会被拒绝；若把「会话上下文资源的 kind」留成待登记，并行 change `migrate-session-context-uri-to-vrn` 的设计就无法落地。所有权在本 change，故必须现在登记而非挂成 open question。会话上下文资源的语义本体是「定位一个 Session」，既有 kind 无一覆盖，故取其语义名 `session`。

**含义**：会话上下文资源的规范形态回到标准资源形态 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/session/{...canonical path segments}`（如 `.../resources/session/{session_id}`），与 `boxteam://session/{session_id}#...` 旧自有语法彻底分道。

**备选**：让会话上下文资源复用 `agent-spec` 或 `skills`（被否：语义完全不匹配，等于用错误词汇掩盖新资源种类）；把该 kind 留成待登记（被否：会阻塞并行 change 落地，且违背本 change 的 kind 所有权）。

### D9：配置来源 VRN 使用 config kind 且只标识可寻址来源文件

**决定**：配置来源 VRN 使用既有 `config` kind，只指向可寻址的来源文件；`layer` 与 `precedence` 保留为 VRN 外的兄弟字段。`inline` 是发行包内稳定文件来源，按 owner requirement 使用 VRN；没有稳定、可寻址来源文件的 `runtime_override` 可不带 VRN，并按资源身份与地址分离的 owner requirement 保持其来源身份。SQLite 仅是内部 carrier，不产生新的资源 kind、地址形式或公开 carrier 字段。

**理由**：VRN 表达来源文件位置，逻辑来源层和存储 carrier 各有职责；为非文件来源造地址会把身份、位置和存储实现重新混在一起。

**备选**：为 runtime override 或 SQLite carrier 新造 kind / VRN（被否：它们不是新增资源类型或来源文件）；把 layer 塞进 VRN（被否：VRN 只承载位置，layer 是并列语义）。

### D10：配置来源迁移直接复用 config 侧既有平级属性模式

`ConfigSourceLayerRecord`（`config/state.py:475`）的 `source_path`/`backup_path` 与 `ConfigSourceJournalRecord.source_path`（`:632`）是已存在的 real path 持久化违约。迁移形态 = 把 `path` 换成 `vrn`，其余 sibling 字段原样保留，与 `ConfigSource`（`config_sources.py:16`）平级结构对齐。

**理由**：用户明确要求「直接复用 config 侧已跑通的模式，不要另发明一套」。

**备选**：为 VRN 单开一个嵌套结构（被否：与 config 侧既有模式分叉，制造第二套表示）。

### D11：正名 builtin 到 inline 且 layer bundled 到 inline，经影响评估后定为安全改名

**理由**：同一概念三个名字（layer `bundled`、scope `builtin`、config layer `inline`）——正是本仓库要求根除的。两处都改名后 shim 退化为恒等映射可删。

**影响评估（已独立核验）**：`layer` 进入 `entry_identity`（`skill_runtime.py:558`）与 catalog payload（`:605`），但二者只进内存 `ResourceRegistry`（`semantic_registry.py:20-23`），无持久化写入；全仓唯一持久化 `display_uri` 列（`resource_activation_schema.py:85`）的写入方 `ResourceActivationStore.persist_snapshot` 生产从不被调用：`app/container.py` 无任何 `resource_activation`/`activation_store` 装配（`rg` 退出 1），`attach_resource_activation_store` 的全部调用方都在 `tests/unit/services/infrastructure/rollout_context/test_resource_activation_{storage,retention,fork_identity}.py`。故**安全改名，无需迁移任务**。真依赖 VRN scope `builtin` 的位置现落在 `grammar.py:18` 的 scope 闭集 `_SCOPE_KEYWORDS` 与 `resolver.py` 的 scope 构造校验（grammar.py 已无 `builtin` 字面量、`_SKILL_SCOPES` 已不存在），其余为无关同名。

**备选**：只改 scope 名不改 layer 名（被否：留下 layer `bundled` 与 config layer `inline` 的同概念异名，且 shim 无法退化为恒等）。

### D12：与其它 change 的接口以「术语冻结 + 单向引用」实现

本 change 是术语、scope、scope_id 语义、语法、kind 闭集、拒绝码与配置来源 layer/precedence 语义的唯一 owner；另外四个 change 引用。

**理由**：用户要求各方不得各自重新定义；物理删除在途 change 的 VRN 定义不可行（其 requirement 已被引用），故采用「引用 + 声明从属」。

**备选**：把 VRN 语法从在途 change 的 spec 中物理删除并全迁到本 change（被否：破坏未归档 change 的 requirement 连续性）。

### D13：config layer 表示逻辑来源，carrier 与快照保持分离

**决定**：config `layer` 只表示逻辑配置来源，闭集与既有 precedence 由 spec requirement「配置 layer 必须表示逻辑来源且与载体和快照分离」唯一登记。读侧将 runtime override 从旧 `sqlite` 值归入逻辑来源 `runtime_override`，保留原 precedence；SQLite 继续作为内部 carrier，不暴露 `sqlite` layer 或新增公开 storage/carrier 字段。`active_snapshot` 与 `pending_snapshot` 是快照状态，不是配置来源，必须位于 `sources[]` 之外。各配置数据 owner 对自己的 `source_key` 映射各自保有唯一权威表；所有读路径一致使用对应映射。

旧开发中间数据不要求兼容：升级实现可以删除并重建不兼容的中间数据，不得为其增加双读、回填或兼容映射。该决定不改变 VRN kind、语法或已有地址形态。

**理由**：`layer` 描述配置为何生效，carrier 描述状态存在哪里，snapshot 描述一次完整配置状态；把这些角色压进同一个 layer 既误报来源，也让 active/pending 状态伪装成可寻址的来源。

## Risks / Trade-offs

- **[契约 v1 曾下发且与真实 grammar 不一致]** → 本 change 全部产物已切到 v2 并按权威表定稿，proposal/design 显式记录变更与理由。
- **[权威表推翻 v2 两处初审]** → 已在 proposal/spec/design 逐处改正（`user`/`inline` 为新增、kind `config` 为新增），避免把初审当定稿。
- **[scope_id 由字面量 local 改为真实身份推导会改变既有字符串]** → `gateway`/`inline` 现共用 `local`，改动产生不同 VRN 字符串；因这些字符串只进内存 registry 与响应、不落盘，属契约级调整而非数据迁移（见 D11）。
- **[破坏性语法改动打断在途实现]** → 迁移计划显式列出旧形态（含 `skill_runtime.py:52/:619` 裸拼接、`:538` 的 shim、layer 名）一并收敛，不留别名或双读；tasks 把「删旧解析实现」与「修消费方」合并为单一步骤，避免悬挂中间态。
- **[跨 gateway 解析引入新失败模式]** → 拒绝码分三套闭合且 fail-closed；对「未授权存在」与「不存在」返回同一结果，避免 locator 泄露；上界为 policy 常量，便于审计。
- **[distribution_id 曾是空洞]** → 权威表证明其全仓零生产赋值；来源已由本 change 定稿为发行 manifest 的 `distribution` + `version`（编码规则见 spec 对应 requirement），空洞从「来源未定」降为「尚未实现装配」，实施期按 D4b 接线。 **（修订注，`298ef599`+`f3bd8213` 落地）**：`load_distribution_id` 已接入 `skill_runtime._layer_scope_identity`，本项由「尚未实现装配」变为已装配。
- **[memory 语义不明可能诱使猜测]** → 已确证它不是 VRN scope，列入 Non-Goals；因其 domain owner 与状态本体从未接入（无 VRN 替代 owner 的需求），解析器侧 MUST 物理移除两点式特例分支并以 `unknown_scope` 类拒绝码 fail-closed 拒绝（**已由提交 32bc6256 落地**）。

## Migration Plan

1. 本 change 落地寻址 capability：术语表、scope 闭集与 scope_id 表、kind 闭集、**分三套**拒绝码集中登记处（spec 层）。
2. 实现统一语法与规范化单一实现，替换 `virtual_resources/grammar.py` 的旧形态；同一步内修正 `skill_runtime.py:52` 与 `:619` 的裸拼接、删除 `:538` 的 `bundled`→`builtin` shim、并把 layer 名同步正名为 `inline`（不暴露中间态）。
3. 把 `distribution_id` 接到已定稿来源（发行 manifest 的 `distribution` + `version`，编码规则见 D4b；已由 `298ef599`+`f3bd8213` 装配），并把 `gateway` 的 `gateway_id` 接到已定稿来源（`identity.json` 的 `load_or_create_gateway_id`，由 Gateway 侧按请求经 `X-BoxTeam-Gateway-Id` 注入，见 D4c）；落实 `gateway`/`inline` 的 scope_id 由真实身份推导。 **（修订注（2026-10-01 第四轮复核更正）：`distribution_id` 部分已由 `298ef599`+`f3bd8213` 落地；`gateway_id` 请求级注入已落地——`64ba30c8`/`53befbfc`/`9881a3b2`）**
4. 接入解析链（本机分支），使 Skill/配置/状态链路改用 VRN 解析，而非仅打印。
5. 按 D10 把配置来源 real path 迁移为 VRN 兄弟字段（`config/state.py` + `config_sources.py` + `api/config.py` 对齐）；layer、precedence、VRN 是否存在、内部 carrier 与 snapshot 边界均按 D13 及其 spec owner requirement 实施。
6. 接入 gateway 层星型解析、policy 常量上界与集中登记的拒绝码。
7. **迁移面**：全部为「加列 + 写路径 + 切读路径」（VRN 零落盘，已确证）；MUST NOT 构造存量扫描或数据改写。
8. 与其它 change 对表：会话上下文 URI 与多工作区复用本 change 的 scope/scope_id/kind 归属；`memory` 在各方均按「非 VRN scope」处理，解析器侧特例分支已物理移除（提交 32bc6256）并以 `unknown_scope` 类拒绝码 fail-closed 拒绝。
9. **回滚策略**：本 change 为规划产物；实施若需回滚，回滚到「旧语法 + 单一打印用途」状态，但必须在同一原子步骤内恢复所有消费方，不得停留半接入态。

## Open Questions

以下是契约层仍需 owner 拍板的点，**不自行放宽**：

1. **gateway authority 与既有 connection_id/gateway_id 的对应（已定稿，不再是 open question）**：v2 要求承载**稳定 gateway_id**；用户已裁定来源为 `${BOXTEAM_HOME}/gateway/identity.json` 的 `load_or_create_gateway_id`（`gateway_<32hex>`），注入 owner 为 Gateway 侧按请求注入，见 D4c 与 spec 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」。剩余仅为实施期装配，不影响契约定稿。
2. **distribution_id 的真实来源（已定稿，不再是 open question）**：用户裁定采用发行 manifest（`packages/launcher/runtime-manifest.schema.json`）的 `distribution` + `version`；编码规则与缺失 fail-closed 见 D4b 与 spec 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」。剩余仅为实施期装配（当前零生产赋值），不影响契约定稿。 **（修订注：已由 `298ef599` 落地装配、`f3bd8213` 收紧编码，不再是待办）**
3. **悬空 VRN 的保留期与 GC 归属**：spec 只要求「悬空是合法值」，未定义留存策略；可能与 Session 删除 tombstone 的恢复窗口相关。
4. **ResourceIdentity 的编码形态**：只要求「不透明、稳定、revision-free」，未限 length/charset 上界；是否由本 change 一并闭合需确认。

## 契约的单一定义点（供四方 change 引用，不复制）

契约的**逐字权威文本**存在于本 change 的规划产物，二者互为唯一来源：

- `openspec/changes/add-unified-virtual-resource-addressing/specs/virtual-resource-addressing/spec.md`：定义三层分离、scope 闭集与必填 scope_id（含 `gateway` scope 的 scope_id 由 Gateway 身份文件按请求注入推导、`inline` scope 由 manifest 推导）、VRN 语法、kind 闭集、gateway authority、星型解析与 policy 常量、**分三套**拒绝码、identity 独立、默认寻址政策与「新写字段」迁移面，以及配置来源 VRN、逻辑 layer/precedence、carrier/snapshot 分离与多工作区显式 scope_id；`builtin`→`inline` 正名归并于相关 scope/layer 定义。
- `openspec/changes/add-unified-virtual-resource-addressing/tasks.md` 任务 `1.1`：术语表登记处；任务 `1.2`：scope 闭集与 scope_id 唯一表落点；任务 `1.3`：**分三套拒绝码集中登记处**（新增/更新拒绝码闭合集的唯一落点）。

实施阶段的机械约束（写入任务，不在本 change 执行）：

1. 拒绝码唯一处：新增码只在任务 `1.3` 指定的登记处出现一次，且 MUST 明确归属 grammar 或 resolve 闭集；其它模块与其它 change 通过引用使用。
2. 术语唯一处：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution` 只在任务 `1.1` 登记；其余 change 只引用。

### 四方接口现状（审计，供用户核对）

- `migrate-session-context-uri-to-vrn`（并行）已声明「不拥有 VRN 语法本体与拒绝码登记」。
- `add-multi-workspace-backend-mounting`（并行）引用了 `资源身份` / `gateway authority` / `real path` / `星型解析` 术语，与契约一致。
- `add-context-injection-lifecycle`（在途未归档）已加「归属声明 / 指向」，其残留旧词汇与 `memory` 形态已就地收口。
- `add-workspace-persistent-resource-management`（第四个 change）持久化领域 owner 的工作区资源记录，持有稳定 `resource_id`（不存 real path）；已加「引用本 change 的 identity + VRN 持久化引用政策、不复制定义」声明。
