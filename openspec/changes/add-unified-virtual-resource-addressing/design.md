## Context

现状（VRN 架构一致性审计，`out/tests/temp/shishan_refactor/artifacts/report-vrn-audit.md`）：

- `app/services/infrastructure/resource_platform/virtual_resources/` 定义了严格 VRN grammar 与 typed resolver，但**解析/授权侧零生产调用**：`parse_vrn`、`VirtualResourceResolver`、`ResolvedResourceHandle`、`ResolutionContext`、`TrackedResourceBinding` 只被测试引用。
- 唯一生产使用点是 `app/agents/skill_runtime.py:541` 的 `skill_display_uri`——即大体系只被用来**打印**地址。
- `skill_runtime.py:52`（`boxteam://workspace/agents`）与 `skill_runtime.py:619`（`boxteam://workspace/{id}/resources/skills/catalog`）是绕过 owner 的裸拼接，实测被自家 grammar 以 `malformed_path` 拒绝。
- `app/services/business/session_context_resource.py` 是**第二套并行**的 `boxteam://` 正则解析器（会话定位，支持 `#fragment`），与 VRN grammar 互不可解析。
- 在途未归档 change `add-context-injection-lifecycle` 的 `tasks.md:47`（3.14）解析器本体 `[x]` 已完成，`tasks.md:95`（6.6）与 `tasks.md:111`（7.1）接线任务 `[ ]` 未做；其 `specs/context-injection-lifecycle/spec.md:331/333` 有 VRN resolver requirement。
- 需求方向见 `proposal.md — Why`；行为契约见 `specs/virtual-resource-addressing/spec.md`。本设计只补「怎么做」。

**契约版本**：本设计采用**契约修正 v2**。v2 相对 v1 的三处语法变更与理由：

1. `{gateway_authority?}` 明确为**可选单段、承载稳定 gateway_id**（v1 未限定段数/取值）。
2. `{scope_id}` 改为**对所有 scope 都必填**（v1 说「可选、只对 workspace」）。**这是原则上更对**，不是妥协：把 scope_id 改成可选，等于对非 workspace 的 scope **重新引入隐含上下文**，正是本次要根除的东西。`scope_id` 全必填即把「workspace_id 必须显式」的原则扩展到所有 scope。
3. 补回 `resources` **固定保留段**（v1 漏了）。

**权威表状态**：scope 名、scope_id 语义、拒绝码名称/数量、`memory` 归属四类，**权威表未下发前为初审状态，不得据此实现**。

## Goals / Non-Goals

**Goals**

- 把「身份 / 地址 / 真实路径」三层分离落成**唯一 owner 与唯一实现**，并给出可机械检查的不变量。
- 给出 v2 统一 VRN 语法（保留 `resources` 固定段序）、规范化单一实现与拒绝码集中登记处的落位。
- 给出顶层 gateway 之间**星型解析**的边界契约（传什么、不传什么、失败怎么表达），并把上界做成显式 policy 常量。
- 登记配置来源 real path 持久化这一**已存在违约**的迁移形态（复用 config 侧既有平级属性模式）。
- 明确与两个并行 change 的接口面，避免三份定义并存。

**Non-Goals**

- 不在本 change 内写生产代码；实施由 tasks 驱动。
- 不设计会话上下文 URI 的统一改造（由并行 change 承载），本 change 只承认其归属与 scope 复用。
- 不设计 HTTP API 的多工作区路由细节（由「单后端多工作区挂载」change 承载），本 change 只要求 scope_id 身份在寻址层显式。
- **不对 `memory` 作用域做任何设计**：其定义与归属由 owner 随后统一下发。
- 不引入资源插件宿主、动态 provider 装配或可安装资源 API。

## Decisions

### D1：三层分离以「谁持久化 / 谁可跨边界」为切分线

- **ResourceIdentity**：持久化，无 revision，不依赖激活工作区 → 用于去重与 lineage。
- **VRN**：持久化，允许悬空，禁 revision/hash → 软件内部与模型可见载荷的默认引用形式。
- **real path**：不持久化、不进模型可见载荷、不跨 gateway → 只在 fs/sqlite 调用点作为局部变量。

**理由**：审计发现的真实缺陷正是「路径形状」泄漏（skill 目录扫描、`skillPaths.ts` 解析物理路径、catalog URI 裸拼接、`ConfigSourceLayerRecord.source_path` 落库）。以「是否持久化」「是否可跨边界」两条可机械判定的性质切分，比按命名切分更抗漂移。

**备选**：把 revision 编进 VRN（被否：revision 变化会让同一资源产生不同地址，破坏「地址稳定、revision 独立」）；把 real path 作为可选调试字段随记录持久化（被否：正是当前要消除的违约，且违反跨 gateway 边界约束）。

### D2：保留既有段序，scope_id 全必填（v2 核心决策）

采用 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...}`，而非简化段序。

**理由**：简化段序（把 `scope_id` 变可选、去掉 `resources`）会让 `user`/`gateway`/`inline` 这些 scope **重新依赖隐含上下文**来补齐身份，与「根除当前工作区隐含前提」的目标直接矛盾；且会与仓库真实 grammar（既有 `resources/{kind}` 段序）分叉，制造第三套形态。保留段序 + scope_id 全必填，是「沿用真实 grammar 且把显式原则推广到所有 scope」。

**备选**：简化段序（被否，见上）；为每个 scope 定制不同段序（被否：多套语法正是本 change 要消除的）。

### D3：authority 缺省 == 本机 gateway == 显式本机 gateway_id

**理由**：既有形态是「authority 缺省」的特例，大量在途规划依赖「不带 authority 即本机」。让显式本机 gateway_id 与缺省**同解**，可避免「本机 vs 显式本机」被当成两个地址而破坏规范化。

**备选**：二者含义不同（被否：产生两个等价地址，持久化记录因写法不同失去可比性）。

### D4：星型解析在 gateway 层收敛，边界只传逻辑事实，上界是 policy 常量

链路：本地 parse（fail-closed）→ 本机按 workspace registry 解析 / 对端按联邦关系转发（hub 直连直接 spoke；spoke 经唯一 hub 一次有界 transit）→ 不可达/未共享/未找到一律 fail-closed。

**理由**：只有实际持有资源的进程能做权威解析；转发方不得猜测路径。把 `visited set`/`max_transit_gateways`/`max_gateway_hops`/总 deadline 做成显式 policy 常量，是因为拓扑会变，解析器不应因拓扑调整而重写。星型（最多一次中继）与仓库既有联邦拓扑一致（`openspec/specs/remote-gateway-federation`）。

**不变量**：`locator 是输入，不是输出`——解析命中只返回稳定身份与内容。

**备选**：转发方缓存并本地解析对端资源（被否：第二份事实源）；对端返回 real path 由本机访问（被否：跨边界传路径，且远端路径在本机无意义）；把上界写死在解析器里（被否：拓扑变化即改代码）。

### D5：拒绝码集中登记，名称待权威表

复用既有 grammar 拒绝码命名空间与风格，全部拒绝码（含跨 gateway 码）在**唯一一处集中登记处**登记；**具体名称与最少数量以权威表为准**，权威表下发前不得自造或定稿。

**理由**：审计显示既有拒绝码已是闭合集（`grammar.py:22-40` 的 18 个码）；集中登记可机械检查「无自造同义码」。名称留给权威表，是因为用户明确要求三方不得各自定义。

**备选**：每个模块自带拒绝码枚举（被否：正是要消除的多份定义）；现在就把跨 gateway 码名写死（被否：违背「权威表下发前不定稿」）。

### D6：identity 与 VRN 的职责边界以「可解析性」划分

identity 不可解析、不做寻址；VRN 可解析、不承担身份。同一逻辑名跨 scope 是两个 identity；跨来源覆盖是独立 concern。

**理由**：审计里 `SemanticResourceDescriptor` 双类同名（`values.py:56` vs `derivation/types.py:26`）与 `contracts.py:26-30` 的 TODO 表明，之前把「身份」与「地址/owner scope」耦合，导致字段语义漂移。以「可解析性」切分最清晰。

**备选**：让 VRN 兼任 identity 用于去重（被否：VRN 允许悬空，悬空后无法去重）。

### D7：配置来源迁移直接复用 config 侧既有平级属性模式

`ConfigSourceLayerRecord`（`config/state.py:473`）的 `source_path`/`backup_path` 是已存在的 real path 持久化违约。迁移形态 = 把 `path` 换成 `vrn`，其余 sibling 字段原样保留，与 `ConfigSource`（`config_sources.py:16`）的 `path` + `layer` + `precedence` + `layer_revision`/`layer_digest`/`source_generation` 平级结构对齐。

**理由**：用户明确要求「直接复用 config 侧已跑通的模式，不要另发明一套」。config 侧的平级属性结构已在用，改造成本与风险最低，且天然满足「real path 只在访问点出现」。

**备选**：为 VRN 单开一个嵌套结构（被否：与 config 侧既有模式分叉，制造第二套表示）。

### D8：与并行 change 的接口以「术语冻结 + 单向引用」实现

本 change 是术语、scope、语法、拒绝码的唯一 owner；另两个 change 引用。在途 `add-context-injection-lifecycle` 的接线任务改指向本 change，并在其 spec 内声明「VRN 语法以本 change 为准」，不复制定义。

**理由**：用户要求三方不得各自重新定义；物理删除其 VRN 定义不可行（该 change 未归档、其 requirement 已被引用），故采用「引用 + 声明从属」。

**备选**：把 VRN 语法从其 spec 中物理删除并全迁到本 change（被否：破坏未归档 change 的 requirement 连续性，且用户只要求「指向 + 声明」）。

## Risks / Trade-offs

- **[契约 v1 曾下发且与真实 grammar 不一致]** → 本 change 全部产物已切到 v2，并在 proposal/design 显式记录三处变更与理由；收口任务要求三方对表。
- **[权威表未下发，四类内容可能再变]** → spec/design 显式标注「初审状态、权威表为准、下发前不得实现」，把不确定性限制在文本层，不进入任何实现依赖。
- **[破坏性语法改动打断在途实现]** → 迁移计划显式列出旧形态（含 `skill_runtime.py:52/:619` 裸拼接、`:538` 的 `builtin` shim）一并收敛，不留别名或双读；tasks 把「删旧解析实现」与「修消费方」合并为单一步骤，避免悬挂中间态（审计已记录此类事故）。
- **[跨 gateway 解析引入新失败模式]** → 拒绝码闭合且 fail-closed；对「未授权存在」与「不存在」返回同一结果，避免 locator 泄露；上界为 policy 常量，便于审计。
- **[配置来源真数据迁移可能影响既有 SQLite]** → 待验证项 1（VRN 是否已落进持久化 session/catalog/checkpoint）确认后，才决定是「新写字段」还是「真数据迁移」；本 change 不断言迁移形态细节。
- **[memory 语义不明可能诱使猜测]** → 明确列入 Non-Goals 与 Open Questions，禁止基于它设计或指派 scope_id。

## Migration Plan

1. 本 change 落地寻址 capability、术语表、scope_id 表与拒绝码集中登记处（spec 层）。
2. 实现 v2 统一语法与规范化单一实现，替换 `virtual_resources/grammar.py` 的旧形态；同一步内修正 `skill_runtime.py:52` 与 `:619` 的裸拼接、删除 `:538` 的 `bundled`→`builtin` shim（不暴露中间态）。
3. 接入解析链（本机分支），使 Skill/配置/状态链路改用 VRN 解析，而非仅打印。
4. 按 D7 把配置来源 real path 迁移为 VRN 兄弟字段（`config/state.py` + `config_sources.py` 对齐）。
5. 接入 gateway 层星型解析、policy 常量上界与集中登记的拒绝码。
6. 与并行 change 对表：会话上下文 URI 复用本 change 的 scope/语法归属；多工作区 change 复用显式 scope_id 身份要求。
7. **回滚策略**：本 change 为规划产物；实施若需回滚，回滚到「旧语法 + 单一打印用途」状态，但必须在同一原子步骤内恢复所有消费方，不得停留半接入态。

## Open Questions

以下是契约层需 owner 拍板的点，**不自行放宽**：

1. **权威表（scope 名 / scope_id 语义 / 拒绝码）下发时点**：v2 已把这三类标注为初审；权威表到达前，本 change 的 tasks 中依赖它们的条目不得启动。
2. **`user` scope 的 `workspace_id` 段语义**：v2 明确 `scope_id` 全必填且 `user`→`local`（已拍板）。需确认「`user` 是否永不细分到 workspace」，即 scope_id 恒为 `local` 而非「可选 workspace_id」。
3. **gateway authority 与既有 `connection_id`/`gateway_id` 的对应**：v2 要求承载**稳定 gateway_id**；需确认稳定 id 的确切来源字段（避免与持久 `connection_id` 或瞬时 channel 标识混淆）。
4. **悬空 VRN 的保留期与 GC 归属**：spec 只要求「悬空是合法值」，未定义留存策略；可能与 Session 删除 tombstone 的恢复窗口相关。
5. **`ResourceIdentity` 的编码形态**：只要求「不透明、稳定、revision-free」，未限 length/charset 上界；是否由本 change 一并闭合需确认。
6. **`memory` 的定义与归属**：明确冻结；需 owner 后续统一下发，本 change 不做设计。
7. **待验证项的处理**：VRN 是否已进持久化数据、`ConfigSource.path` 是否进 API 响应体、`inline`/`sqlite` 是否有可解析载体——确认结果可能改变迁移任务的范围（新写字段 vs 真数据迁移），需 owner 反馈。
8. **在途 change 的旧形态样例句**：`add-context-injection-lifecycle` 的 VRN requirement 正文仍保留旧样例如 `boxteam://workspace/{workspace_id}/resources/agent-spec/root/AGENTS.md`（`resources/{kind}` 段位）。本 change 只加了归属声明、**未改其行为描述正文**（避免越权改他人 change）。请裁定：授权本 change 后续同步为新语法，还是由该作者自行修订。

## 契约的单一定义点（供三方 change 引用，不复制）

v2 契约的**逐字权威文本**存在于本 change 的规划产物，二者互为唯一来源：

- `openspec/changes/add-unified-virtual-resource-addressing/specs/virtual-resource-addressing/spec.md`：10 条 requirement / 38 条 scenario，定义三层分离、scope 与必填 scope_id、v2 VRN 语法、gateway authority、星型解析与 policy 常量、拒绝码集中登记、identity 独立、默认寻址政策、配置来源 VRN 迁移、多工作区显式 scope_id。
- `openspec/changes/add-unified-virtual-resource-addressing/tasks.md` 任务 `1.1`：术语表登记处；任务 `1.2`：**拒绝码集中登记处**（实现阶段新增/更新拒绝码闭合集的唯一落点）；任务 `1.3`：scope_id 唯一表落点。

实施阶段的两个机械约束（写入任务，不在本 change 执行）：

1. 拒绝码唯一处：新增码只在任务 `1.2` 指定的登记处出现一次，其它模块与其它 change 通过引用使用；未登记码由闭合校验判定为实现缺陷。
2. 术语唯一处：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution` 只在任务 `1.1` 登记；`migrate-session-context-uri-to-vrn` 与 `add-multi-workspace-backend-mounting` 只引用。

### 三方接口现状（审计，供用户核对）

- `migrate-session-context-uri-to-vrn`（并行）已声明「不拥有 VRN 语法本体与拒绝码登记——由『统一虚拟资源寻址』change 独占」。
- `add-multi-workspace-backend-mounting`（并行）引用了 `资源身份` / `gateway authority` / `real path` / `星型解析` 术语，与契约一致。
- `add-context-injection-lifecycle`（在途未归档）已由本 change 在 `tasks.md` 的 `3.14`/`6.6`/`7.1` 三处加「归属声明 / 指向」，并在其 `specs/context-injection-lifecycle/spec.md` 的 VRN requirement 顶部加归属声明。**其 requirement 正文中的旧形态示例保留未改**——见 Open Questions 第 8 点。
