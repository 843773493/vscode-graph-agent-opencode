## Context

现状（VRN 权威表实测取证，`out/tests/temp/p1_fixer_identity/artifacts/report-vrn-authority.md`；以及架构一致性审计 `out/tests/temp/shishan_refactor/artifacts/report-vrn-audit.md`）：

- `app/services/infrastructure/resource_platform/virtual_resources/` 定义了严格 VRN grammar 与 typed resolver，但**解析/授权侧零生产调用**：`parse_vrn`、`VirtualResourceResolver`、`ResolvedResourceHandle`、`ResolutionContext` 只被测试引用。
- 唯一生产使用点是 `app/agents/skill_runtime.py:541` 的 `skill_display_uri`——即大体系只被用来**打印**地址。
- `skill_runtime.py:52`（`boxteam://workspace/agents`）与 `skill_runtime.py:619`（`boxteam://workspace/{id}/resources/skills/catalog`）是绕过 owner 的裸拼接，实测被自家 grammar 以 `malformed_path` 拒绝；`boxteam://gateway/...` 等会话定位串同样被拒。
- `app/services/business/session_context_resource.py` 是**第二套并行**的 `boxteam://` 正则解析器（会话定位，支持 `#fragment`），与 VRN grammar 互不可解析。
- 在途未归档 change `add-context-injection-lifecycle` 的 `tasks.md:47`（3.14）解析器本体已完成，`tasks.md:95`（6.6）与 `:111`（7.1）接线任务未做；其 `specs/context-injection-lifecycle/spec.md:331/333` 有 VRN resolver requirement。

**契约版本与权威表状态**：本设计采用**契约修正 v2**（保留既有段序，`resources` 为固定保留段，`scope_id` 对所有 scope 必填），并已依据**已下发的权威表**定稿三类内容：scope 闭集、scope_id 语义、kind 闭集与拒绝码登记。权威表推翻 v2 两处初审：既有 scope 闭集其实只有 `workspace`/`gateway`/`builtin`/`memory`，`user`/`inline` 为新增；kind 既有只有 `agent-spec`/`skills`，`config` 与 `session` 为新增。

## Goals / Non-Goals

**Goals**

- 把「身份 / 地址 / 真实路径」三层分离落成**唯一 owner 与唯一实现**，并给出可机械检查的不变量。
- 给出一套统一 VRN 语法（保留 `resources` 固定段序）与规范化单一实现、**定稿**的 scope 闭集、scope_id 语义表与 kind 闭集。
- 给出**分两套**集中登记的拒绝码（grammar 17 + resolve 6）与其不混用约束。
- 给出顶层 gateway 之间**星型解析**的边界契约，并把上界做成显式 policy 常量。
- 登记配置来源 real path 持久化这一**已存在违约**的迁移形态，并说明 `sqlite` 层为何无 VRN。
- 明确与另外三个 change 的接口面，避免多份定义并存。

**Non-Goals**

- 不在本 change 内写生产代码；实施由 tasks 驱动。
- 不设计会话上下文 URI 的统一改造（由并行 change 承载），本 change 只承认其归属与 scope/kind 复用。
- 不设计 HTTP API 的多工作区路由细节（由「单后端多工作区挂载」change 承载），本 change 只要求 scope_id 身份在寻址层显式。
- **不为 `memory` 做任何设计**：它已确证不是 VRN scope；既有两点式形态只作非 VRN 示意。
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

**理由**：权威表实测既有闭集只有 `workspace`/`gateway`/`builtin`/`memory`；`user` 是用户要求新增。`memory` 零生产构造方、resolver 不比对 scope_id、`kind="memory"` 零构造、container 未装配，且两点式形态（无 `resources`、无 kind）会绕过固定段与 kind 校验——把它当 scope 等于在统一语法上开后门。故移出并显式标注「`memory` 不是 VRN scope」。

**备选**：保留 `memory` 于闭集并为其定义 scope_id（被否：无任何事实支撑，会固化一个空洞）；把 `memory` 两点式当合法特例（被否：破坏「单一语法」，即本次要根除的双轨）。

### D4：scope_id 必须由真实身份推导，禁止硬编码

**理由**：现状 `skill_runtime.py:539` 把 `gateway` 与 `builtin` 的 scope_id 都硬编码为 `local`，二者逐字相同；resolver 期望的 `distribution_id` 全仓零生产赋值。让 `gateway`→真实 gateway_id、`inline`→真实 distribution_id，才能让 scope_id 真正承担身份，否则「必填」只是形式。`user`→`local` 依据 AGENTS.md 的单用户本地程序前提。

**备选**：继续用 `local` 字面量（被否：scope_id 失去区分能力，且 `inline` 与 `gateway` 冲突）。

### D5：authority 缺省 == 本机 gateway == 显式本机 gateway_id

**理由**：既有形态是「authority 缺省」的特例，大量在途规划依赖「不带 authority 即本机」。让显式本机 gateway_id 与缺省**同解**，可避免「本机 vs 显式本机」被当成两个地址而破坏规范化。

**备选**：二者含义不同（被否：产生两个等价地址，持久化记录因写法不同失去可比性）。

### D6：星型解析在 gateway 层收敛，边界只传逻辑事实，上界是 policy 常量

链路：本地 parse（fail-closed）→ 本机按 workspace registry 解析 / 对端按联邦关系转发（hub 直连直接 spoke；spoke 经唯一 hub 一次有界 transit）→ 不可达/未共享/未找到一律 fail-closed。

**理由**：只有实际持有资源的进程能做权威解析；转发方不得猜测路径。把 `visited set`/`max_transit_gateways`/`max_gateway_hops`/总 deadline 做成显式 policy 常量，是因为拓扑会变，解析器不应因拓扑调整而重写。星型（最多一次中继）与仓库既有联邦拓扑一致。

**不变量**：`locator 是输入，不是输出`。

**备选**：转发方缓存并本地解析对端资源（被否：第二份事实源）；对端返回 real path 由本机访问（被否：跨边界传路径，且远端路径在本机无意义）；把上界写死在解析器里（被否：拓扑变化即改代码）。

### D7：拒绝码分两套集中登记，不可混用

**理由**：权威表实测存在**两个独立闭集**——`VrnGrammarError.reason_code` 17 个（`grammar.py:22-42`，构造函数拒绝未登记 code）与 `_RESOLVE_REASON_CODES` 6 个（`resolver.py:29-38`）。它们分别对应「字符串→ParsedVrn 的语法期」与「已解析后的解析/授权期」，混用会让阶段职责错位。集中登记可机械检查「无自造同义码」。

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

### D9：配置来源寻址用 config kind，sqlite 层不编 VRN

**理由**：`inline` 层是发行包内真实文件（`is_file()` 校验），有稳定 disk 载体；`sqlite` 层是共享同一 `workspace.sqlite` 的**边界变量**，单 VRN 会对应四个逻辑来源。`layer` 保留为兄弟字段，与 config 侧既有平级属性一致。

**备选**：为每层各编一个 VRN（被否：`sqlite` 层物理同文件，编造出「同一 URI 对应多来源」）；把 layer 塞进 VRN（被否：VRN 只承载位置，layer 是并列语义）。

### D10：配置来源迁移直接复用 config 侧既有平级属性模式

`ConfigSourceLayerRecord`（`config/state.py:475`）的 `source_path`/`backup_path` 与 `ConfigSourceJournalRecord.source_path`（`:632`）是已存在的 real path 持久化违约。迁移形态 = 把 `path` 换成 `vrn`，其余 sibling 字段原样保留，与 `ConfigSource`（`config_sources.py:16`）平级结构对齐。

**理由**：用户明确要求「直接复用 config 侧已跑通的模式，不要另发明一套」。

**备选**：为 VRN 单开一个嵌套结构（被否：与 config 侧既有模式分叉，制造第二套表示）。

### D11：正名 builtin 到 inline 且 layer bundled 到 inline，经影响评估后定为安全改名

**理由**：同一概念三个名字（layer `bundled`、scope `builtin`、config layer `inline`）——正是本仓库要求根除的。两处都改名后 shim 退化为恒等映射可删。

**影响评估（已独立核验）**：`layer` 进入 `entry_identity`（`skill_runtime.py:558`）与 catalog payload（`:605`），但二者只进内存 `ResourceRegistry`（`semantic_registry.py:20-23`），无持久化写入；全仓唯一持久化 `display_uri` 列（`resource_activation_schema.py:85`）的写入方 `ResourceActivationStore.persist_snapshot` 生产从不被调用（磁盘取证唯一命中是测试夹具）。故**安全改名，无需迁移任务**。真依赖 VRN scope `builtin` 的位置只有 `resolver.py:205`、`grammar.py:17/19/224`，其余为无关同名。

**备选**：只改 scope 名不改 layer 名（被否：留下 layer `bundled` 与 config layer `inline` 的同概念异名，且 shim 无法退化为恒等）。

### D12：与其它 change 的接口以「术语冻结 + 单向引用」实现

本 change 是术语、scope、scope_id 语义、语法、kind 闭集与拒绝码的唯一 owner；另外四个 change 引用。

**理由**：用户要求各方不得各自重新定义；物理删除在途 change 的 VRN 定义不可行（其 requirement 已被引用），故采用「引用 + 声明从属」。

**备选**：把 VRN 语法从在途 change 的 spec 中物理删除并全迁到本 change（被否：破坏未归档 change 的 requirement 连续性）。

## Risks / Trade-offs

- **[契约 v1 曾下发且与真实 grammar 不一致]** → 本 change 全部产物已切到 v2 并按权威表定稿，proposal/design 显式记录变更与理由。
- **[权威表推翻 v2 两处初审]** → 已在 proposal/spec/design 逐处改正（`user`/`inline` 为新增、kind `config` 为新增），避免把初审当定稿。
- **[scope_id 由字面量 local 改为真实身份推导会改变既有字符串]** → `gateway`/`inline` 现共用 `local`，改动产生不同 VRN 字符串；因这些字符串只进内存 registry 与响应、不落盘，属契约级调整而非数据迁移（见 D11）。
- **[破坏性语法改动打断在途实现]** → 迁移计划显式列出旧形态（含 `skill_runtime.py:52/:619` 裸拼接、`:538` 的 shim、layer 名）一并收敛，不留别名或双读；tasks 把「删旧解析实现」与「修消费方」合并为单一步骤，避免悬挂中间态。
- **[跨 gateway 解析引入新失败模式]** → 拒绝码分两套闭合且 fail-closed；对「未授权存在」与「不存在」返回同一结果，避免 locator 泄露；上界为 policy 常量，便于审计。
- **[distribution_id 是空洞]** → 权威表证明其全仓零生产赋值；`inline` scope_id 要落真实来源，MUST 在实施期先建立该来源，否则 scope_id 仍是假值（列入 tasks 的显式前置）。
- **[memory 语义不明可能诱使猜测]** → 已确证它不是 VRN scope，列入 Non-Goals 并明确「移出闭集 + 两点式只作非 VRN 示意」。

## Migration Plan

1. 本 change 落地寻址 capability：术语表、scope 闭集与 scope_id 表、kind 闭集、**分两套**拒绝码集中登记处（spec 层）。
2. 实现统一语法与规范化单一实现，替换 `virtual_resources/grammar.py` 的旧形态；同一步内修正 `skill_runtime.py:52` 与 `:619` 的裸拼接、删除 `:538` 的 `bundled`→`builtin` shim、并把 layer 名同步正名为 `inline`（不暴露中间态）。
3. 建立 `distribution_id` 的真实来源（当前零赋值），落实 `gateway`/`inline` 的 scope_id 由真实身份推导。
4. 接入解析链（本机分支），使 Skill/配置/状态链路改用 VRN 解析，而非仅打印。
5. 按 D10 把配置来源 real path 迁移为 VRN 兄弟字段（`config/state.py` + `config_sources.py` + `api/config.py` 对齐）；`sqlite` 层显式不编 VRN。
6. 接入 gateway 层星型解析、policy 常量上界与集中登记的拒绝码。
7. **迁移面**：全部为「加列 + 写路径 + 切读路径」（VRN 零落盘，已确证）；MUST NOT 构造存量扫描或数据改写。
8. 与其它 change 对表：会话上下文 URI 与多工作区复用本 change 的 scope/scope_id/kind 归属；`memory` 在各方均按「非 VRN scope」处理。
9. **回滚策略**：本 change 为规划产物；实施若需回滚，回滚到「旧语法 + 单一打印用途」状态，但必须在同一原子步骤内恢复所有消费方，不得停留半接入态。

## Open Questions

以下是契约层仍需 owner 拍板的点，**不自行放宽**：

1. **gateway authority 与既有 connection_id/gateway_id 的对应**：v2 要求承载**稳定 gateway_id**；需确认稳定 id 的确切来源字段（避免与持久 `connection_id` 或瞬时 channel 标识混淆）。
2. **distribution_id 的真实来源**：权威表证明其全仓零生产赋值；需 owner 指定由发行 manifest（`packages/launcher/runtime-manifest.schema.json:9` 的 `distribution` 字段）还是其它稳定字段承接。
3. **悬空 VRN 的保留期与 GC 归属**：spec 只要求「悬空是合法值」，未定义留存策略；可能与 Session 删除 tombstone 的恢复窗口相关。
4. **ResourceIdentity 的编码形态**：只要求「不透明、稳定、revision-free」，未限 length/charset 上界；是否由本 change 一并闭合需确认。

## 契约的单一定义点（供四方 change 引用，不复制）

契约的**逐字权威文本**存在于本 change 的规划产物，二者互为唯一来源：

- `openspec/changes/add-unified-virtual-resource-addressing/specs/virtual-resource-addressing/spec.md`：定义三层分离、scope 闭集与必填 scope_id、VRN 语法、kind 闭集、gateway authority、星型解析与 policy 常量、**分两套**拒绝码、identity 独立、默认寻址政策与「新写字段」迁移面、配置来源 VRN 迁移与 `sqlite` 不可寻址、多工作区显式 scope_id、`builtin`→`inline` 正名（含 layer）。
- `openspec/changes/add-unified-virtual-resource-addressing/tasks.md` 任务 `1.1`：术语表登记处；任务 `1.2`：scope 闭集与 scope_id 唯一表落点；任务 `1.3`：**分两套拒绝码集中登记处**（新增/更新拒绝码闭合集的唯一落点）。

实施阶段的机械约束（写入任务，不在本 change 执行）：

1. 拒绝码唯一处：新增码只在任务 `1.3` 指定的登记处出现一次，且 MUST 明确归属 grammar 或 resolve 闭集；其它模块与其它 change 通过引用使用。
2. 术语唯一处：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution` 只在任务 `1.1` 登记；其余 change 只引用。

### 四方接口现状（审计，供用户核对）

- `migrate-session-context-uri-to-vrn`（并行）已声明「不拥有 VRN 语法本体与拒绝码登记」。
- `add-multi-workspace-backend-mounting`（并行）引用了 `资源身份` / `gateway authority` / `real path` / `星型解析` 术语，与契约一致。
- `add-context-injection-lifecycle`（在途未归档）已加「归属声明 / 指向」，其残留旧词汇与 `memory` 形态已就地收口。
- `add-workspace-persistent-resource-management`（第四个 change）持久化领域 owner 的工作区资源记录，持有稳定 `resource_id`（不存 real path）；已加「引用本 change 的 identity + VRN 持久化引用政策、不复制定义」声明。
