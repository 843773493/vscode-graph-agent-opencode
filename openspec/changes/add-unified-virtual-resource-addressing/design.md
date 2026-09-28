## Context

现状（VRN 架构一致性审计，`out/tests/temp/shishan_refactor/artifacts/report-vrn-audit.md`）：

- `app/services/infrastructure/resource_platform/virtual_resources/` 定义了严格 VRN grammar 与 typed resolver，但**解析/授权侧零生产调用**：`parse_vrn`、`VirtualResourceResolver`、`ResolvedResourceHandle`、`ResolutionContext`、`TrackedResourceBinding` 只被测试引用。
- 唯一生产使用点是 `app/agents/skill_runtime.py:541` 的 `skill_display_uri`——即大体系只被用来**打印**地址。
- `skill_runtime.py:52`（`boxteam://workspace/agents`）与 `skill_runtime.py:619`（`boxteam://workspace/{id}/resources/skills/catalog`）是绕过 owner 的裸拼接，实测被自家 grammar 以 `malformed_path` 拒绝。
- `app/services/business/session_context_resource.py` 是**第二套并行**的 `boxteam://` 正则解析器（会话定位，支持 `#fragment`），与 VRN grammar 互不可解析。
- 在途未归档 change `add-context-injection-lifecycle` 的 `tasks.md:47`（3.14）解析器本体 `[x]` 已完成，`tasks.md:95`（6.6）与 `tasks.md:111`（7.1）接线任务 `[ ]` 未做；其 `specs/context-injection-lifecycle/spec.md:331/333` 有 VRN resolver requirement。
- 需求方向见 `proposal.md — Why`；行为契约见 `specs/virtual-resource-addressing/spec.md`。本设计只补「怎么做」。

## Goals / Non-Goals

**Goals**

- 把「身份 / 地址 / 真实路径」三层分离落成**唯一 owner 与唯一实现**，并给出可机械检查的不变量。
- 给出统一 VRN 语法、规范化单一实现与闭合拒绝码登记处的落位。
- 给出顶层 gateway 之间**星型解析**的边界契约（传什么、不传什么、失败怎么表达）。
- 明确与两个并行 change 的接口面，避免三份定义并存。

**Non-Goals**

- 不在本 change 内写生产代码；实施由 tasks 驱动。
- 不设计会话上下文 URI 的统一改造（由并行 change 承载），本 change 只承认其归属与 scope 复用。
- 不设计 HTTP API 的多工作区路由细节（由「单后端多工作区挂载」change 承载），本 change 只要求 workspace 身份在寻址层显式。
- 不引入资源插件宿主、动态 provider 装配或可安装资源 API。

## Decisions

### D1：三层分离以「谁持久化 / 谁可跨边界」为切分线

- **ResourceIdentity**：持久化，无 revision，不依赖激活工作区 → 用于去重与 lineage。
- **VRN**：持久化，允许悬空，禁 revision/hash → 软件内部与模型可见载荷的默认引用形式。
- **real path**：不持久化、不进模型可见载荷、不跨 gateway → 只在 fs/sqlite 调用点作为局部变量。

**理由**：审计发现的真实缺陷正是「路径形状」泄漏（skill 目录扫描、`skillPaths.ts` 解析物理路径、catalog URI 裸拼接）。以「是否持久化」「是否可跨边界」两条可机械判定的性质切分，比按命名切分更抗漂移。

**备选**：把 revision 编进 VRN（被否：revision 变化会让同一资源产生不同地址，破坏「地址稳定、revision 独立」）；把 real path 作为可选调试字段随记录持久化（被否：违反「永不返回虚假路径」与跨 gateway 边界约束）。

### D2：scope 闭合为 3 值，跨工作区与跨 gateway 都不新增 scope

- 其它工作区 = `workspace` scope + 另一个 `workspace_id`。
- 其它 gateway = 可选 **gateway authority** 段。

**理由**：scope 表达「属于谁的数据域」（用户级 / 工作区级 / Gateway 控制面），gateway 是**拓扑维度**，与数据域正交。混入 scope 会让 scope 集随拓扑扩张而膨胀，且「workspace」这个词在跨 gateway 时会歧义（哪个 gateway 的 workspace）。

**备选**：新增 `remote-workspace` / `peer` scope（被否：scope 语义污染 + 与并行 change 术语冲突）；用 VRN 前缀区分（被否：即「第二套语法」）。

### D3：authority 缺省 == 本机 == self，三者同解

**理由**：审计显示旧形态是「authority 缺省」的特例，且大量在途规划（`add-itemized-rollout-context` 的 session URI）依赖「不带 authority 即本机」。让 self 显式写出来与缺省**同解**，可以避免「本机 vs self」被当成两个地址。

**备选**：self 与缺省含义不同（被否：会产生两个等价地址，破坏规范化；且会让持久化记录因写法不同而失去可比性）。

### D4：星型解析在 gateway 层收敛，边界只传逻辑事实

链路：本地 parse（fail-closed）→ 本机按 workspace registry 解析 / 对端交 gateway 层转发并由**对端本地解析** → 不可达、未共享、未找到一律 fail-closed。

**理由**：只有实际持有资源的进程能做权威解析；转发方不得猜测路径。星型（最多一次中继）与仓库既有联邦拓扑约束一致（`openspec/specs/remote-gateway-federation`、`add-itemized-rollout-context` 的 hub-and-spoke 规定）。

**备选**：转发方缓存并本地解析对端资源（被否：第二份事实源，违反前端状态与 catalog 权威原则）；对端返回 real path 由本机访问（被否：跨边界传路径，且远端路径在本机无意义）。

### D5：拒绝码集中登记于寻址 capability 的唯一处

复用既有 grammar 拒绝码命名空间与风格，只新增 `unknown_gateway` / `remote_unreachable` / `remote_not_shared` 三个，集中在寻址 capability 的唯一登记处；其它 change 只引用。

**理由**：审计发现拒绝码已是闭合集（`grammar.py:22-40` 的 18 个码），跨 gateway 只缺 3 个语义；集中登记可机械检查「无自造同义码」。

**备选**：每个模块自带拒绝码枚举（被否：正是要消除的多份定义）；把跨 gateway 失败映射为通用 `unknown_resource`（被否：丢失可诊断性，违反「抛出尽可能详细的错误」）。

### D6：identity 与 VRN 的职责边界以「可解析性」划分

identity 不可解析、不做寻址；VRN 可解析、不承担身份。同一逻辑名跨 scope 是两个 identity；跨来源覆盖是独立 concern。

**理由**：审计里 `SemanticResourceDescriptor` 双类同名（`values.py:56` vs `derivation/types.py:26`）与 `contracts.py:26-30` 的 TODO 表明，之前把「身份」与「地址/owner scope」耦合在一起，导致字段语义漂移。以「可解析性」切分最清晰。

**备选**：让 VRN 兼任 identity 用于去重（被否：VRN 允许悬空，悬空后无法去重）。

### D7：与并行 change 的接口以「术语冻结 + 单向引用」实现

本 change 是术语、scope、语法、拒绝码的唯一 owner；另两个 change 引用。在途 `add-context-injection-lifecycle` 的接线任务改指向本 change，并在其 spec 内声明「VRN 语法以本 change 为准」，不复制定义。

**理由**：用户提供的冻结契约 v1 明确要求三方不得各自重新定义；物理删除其 VRN 定义不可行（该 change 未归档、其 requirement 已被引用），故采用「引用 + 声明从属」。

**备选**：把 VRN 语法从 `add-context-injection-lifecycle` 中物理删除并全部迁到本 change（被否：会破坏一个未归档 change 的 requirement 连续性，且用户只要求「指向 + 声明」，未要求搬迁）。

## Risks / Trade-offs

- **[破坏性语法改动打断在途实现]** → 迁移计划显式列出旧形态（含 `skill_runtime.py:52/:619` 裸拼接）一并收敛，不提供别名或双读；tasks 中把「删除旧解析实现」与「修消费方」合并为单一步骤，避免中间悬挂态（审计已记录此类事故）。
- **[跨 gateway 解析引入新失败模式]** → 三个新增拒绝码闭合，且 fail-closed；对「未授权存在」与「不存在」返回同一结果，避免 locator 泄露。
- **[authority 缺省 == self 可能与既有数据不一致]** → 旧数据均为缺省本机形态，语义不变；self 是新增显式写法，不改变既有记录含义。
- **[多工作区未显式化时会静默用激活工作区]** → 用 req「workspace 路径必须显式携带 workspace_id」+ 一条可测场景把隐式补齐判为失败。
- **[contract 歧义：三层命名与 scope 命名等需用户确认的点]** → 见 Open Questions，不自行放宽。

## Migration Plan

1. 本 change 落地寻址 capability、术语表与拒绝码登记处（spec 层）。
2. 实现统一语法与规范化单一实现，替换 `virtual_resources/grammar.py` 的旧形态；同一步内修正 `skill_runtime.py:52` 与 `:619` 的裸拼接（不暴露中间态）。
3. 接入解析链（本机分支），使 Skill/配置/状态链路改用 VRN 解析，而非仅打印。
4. 接入 gateway 层星型解析与三个跨边界拒绝码。
5. 与并行 change 对表：会话上下文 URI 复用本 change 的 scope/语法归属；多工作区 change 复用显式 workspace 身份要求。
6. **回滚策略**：本 change 为规划产物；实施若需回滚，回滚到「旧语法 + 单一打印用途」状态，但必须在同一原子步骤内恢复所有消费方，不得停留半接入态。

## Open Questions

以下是契约层可能需用户拍板的点，**不自行放宽**：

1. `user` scope 的 `workspace_id` 段是「必须缺省」还是「必须不出现」——本设计按「不出现（`user` 无 workspace 维度）」登记，需确认是否会与用户级按工作区隔离数据的需求冲突。
2. gateway authority 的**取值空间**（稳定 `gateway_id`？配置连接 ID？主机名？）——本设计只要求「本机 self / 对端可区分」，具体标识沿用 Gateway 既有 registry identity，需确认。
3. 悬空 VRN 的**保留期**与是否可被 GC——spec 只要求「悬空是合法值」，未定义留存策略；可能与 Session 删除 tombstone 的恢复窗口相关，需确认归属。
4. `ResourceIdentity` 的**编码形态**（不透明字符串是否要限定 length/charset 上界）——本设计只要求「不透明、稳定、revision-free」，未定型，需确认是否由本 change 一并闭合。
5. `add-context-injection-lifecycle` 的 VRN requirement 正文里保留了旧形态样例（如 `boxteam://workspace/{workspace_id}/resources/agent-spec/root/AGENTS.md`，即 `resources/{kind}` 段位）。本 change 已在该 requirement 顶部加归属声明，但**该样例会随新语法一起过时**。是否授权我在后续（或本 change 的收口任务内）把那些样例句同步为新语法，还是由该 change 的作者自行修订——需用户拍板，避免我越权改另一个 change 的行为描述。


## 契约的单一定义点（供三方 change 引用，不复制）

冻结契约 v1 的**逐字权威文本**不存在于实现代码，而存在于本 change 的两处规划产物，二者互为唯一来源：

- `openspec/changes/add-unified-virtual-resource-addressing/specs/virtual-resource-addressing/spec.md`：9 条 requirement / 28 条 scenario，定义三层分离、scope 闭合集、VRN 语法、gateway authority、星型解析、拒绝码、identity 独立、默认寻址政策、多工作区显式身份。
- `openspec/changes/add-unified-virtual-resource-addressing/tasks.md` 任务 `1.1`：术语表登记处；任务 `1.2`：**拒绝码集中登记处**（实现阶段该登记处是新增/更新拒绝码闭合集的唯一落点）。

实施阶段的两个机械约束（写入任务，不在本 change 执行）：

1. 拒绝码唯一处：新增码只在任务 `1.2` 指定的登记处出现一次，其它模块与其它 change 通过引用使用；未登记码由闭合校验判定为实现缺陷。
2. 术语唯一处：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution` 只在任务 `1.1` 登记；`migrate-session-context-uri-to-vrn` 与 `add-multi-workspace-backend-mounting` 只引用。

### 三方接口现状（审计，供用户核对）

- `migrate-session-context-uri-to-vrn`（并行）已正确声明「不拥有 VRN 语法本体与拒绝码登记——由『统一虚拟资源寻址』change 独占」。
- `add-multi-workspace-backend-mounting`（并行）引用了 `资源身份` / `gateway authority` / `real path` / `星型解析` 术语，与契约一致；但它当前 `openspec validate --strict` 报 `Change must have at least one delta`（尚未建 specs 目录），**属于该 change 的在途状态，非本 change 缺陷**。
- `add-context-injection-lifecycle`（在途未归档）已由本 change 在 `tasks.md` 的 `3.14`/`6.6`/`7.1` 三处加「归属声明 / 指向」，并在其 `specs/context-injection-lifecycle/spec.md` 的 VRN requirement 顶部加归属声明，声明语法/scope/拒绝码以本 change 为准。**其 requirement 正文中的旧形态示例（如 `boxteam://workspace/{workspace_id}/resources/agent-spec/root/AGENTS.md`）保留未改**——见 Open Questions 第 5 点。
