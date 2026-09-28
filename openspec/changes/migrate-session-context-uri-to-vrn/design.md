## Context

见 `proposal.md` 的 Why。此处只补充塑造方案所需的现状与约束。

当前会话上下文资源的真实形态（`app/services/business/session_context_resource.py`）：

- 三个自有正则 `_SESSION_RESOURCE` / `_WORKSPACE_SESSION_RESOURCE` / `_WORKSPACE_SESSIONS_RESOURCE` 各自 `fullmatch` 一个字符串；
- `parse_session_context_resource(resource)` 先按 `#` 切出 `base` 与 `selector`，selector 只允许 `information` / `assembly={id}` / `record={index}` 三种；
- `ParsedSessionContextResource` 字段为 `(canonical, base, kind, session_id, workspace_id, selector)`；
- `SessionContextCursorCodec` 把 `{resource, revision, operation, offset, char_offset}` 做 base64 游标，`decode` 时校验 `revision` 与 `resource`/`operation` 一致；
- `expected_revision` 已作为**独立参数**存在（`ReadContextInput.expected_revision`），`require_session_context_revision` 在不匹配时抛显式修订变更错误。

与此并存的资源平台 VRN（`resource_platform/virtual_resources/`）实测形态：

- `parse_vrn` 只接受 `boxteam://{scope}/...`，scope 闭集 `{workspace, gateway, builtin, memory}`，`kind` 闭集 `{agent-spec, skills}`；
- 整体拒绝 `%`（`percent_encoding_rejected`）与 `#`（`fragment_rejected`）；
- 双向交叉解析**实测 100% 互斥**：VRN 拒绝全部 6 条上下文 URI；上下文 parser 拒绝全部 3 条 VRN 地址。

因此冲突的**技术根因是两种信息被塞进同一个字符串**：上下文侧把「资源位置」与「修订/视图绑定」编在一起，而 VRN 明确只承担前者。

### 约束（冻结契约 v2）

- VRN **禁止编码 revision/hash**；identity 独立于 VRN；`real path` 永不持久化 / 永不进模型可见载荷 / 永不跨 gateway。
- 权威 VRN 形态（保留既有段序，**不得简化**）：`boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}`。`resources` 固定段保留；`scope_id` 对**所有** scope 都必填。
- scope 为闭合集（`workspace`/`user`/`gateway`/`inline`/`memory`；`builtin` 正名为 `inline`）；scope 名与 scope_id 语义以「统一虚拟资源寻址」change 的**权威表**为准。
- 星型解析唯一顺序，上界为显式策略常量；不可达/未共享/未找到 fail-closed 结构化拒绝码；**locator 是输入不是输出**。
- 拒绝码命名空间由「统一虚拟资源寻址」change 集中登记，本 change **只能引用不能自造**；VRN 语法本体与固定段序同样不由本 change 拥有。
- 命名必须逐字使用：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`。
- 不基于未重新定义的 scope（`memory`）做设计；不为其规定 scope_id。

## Goals / Non-Goals

**Goals:**

- 把会话上下文从「两套并行语法」变成「一套资源语法 + 一层修订绑定」。
- 论证并落地「VRN 不编码 revision」与「上下文可重读修订 locator」**同时成立**。
- 给出破坏性迁移与回滚边界。
- 收口在途 change `add-itemized-rollout-context` 的会话上下文 URI requirement，消除两套定义并存。

**Non-Goals:**

- 不定义 VRN 语法本体、scope 关键字集合、kind 闭集与拒绝码登记（归「统一虚拟资源寻址」change）。
- 不实现任何生产代码；本 change 只产出规划产物。
- 不设计「单后端多工作区挂载」（归另一个并行 change）。
- 不改变会话上下文的**业务语义**（读什么视图、投影边界、权限判定），只改变其**寻址表示与解析**。
- 不引入 `virtual url` / `VURI` 等同义异名。

## Decisions

### D1：位置归 VRN，修订与视图归结构化兄弟字段（本 change 的关键论证）

**决定**：会话上下文引用的表示从「一个字符串」改为**一个结构化引用**，其形态为：

```text
SessionContextResourceRef {
    resource_identity : ResourceIdentity   # 不透明、稳定、revision-free、不依赖激活工作区
    vrn               : VRN                # 位置，唯一可解析地址，禁止编码 revision/hash
    scope             : <scope 闭集值>          # 与 vrn 内 scope 一致，冗余可校验
    revision          : str | None         # 兄弟字段：期望修订绑定
    view              : <闭合枚举> | None  # 兄弟字段：原 fragment 承载的视图选择
    cursor            : str | None         # 兄弟字段：分页游标（内部已绑定 resource+revision+operation）
}
```

**为什么两个约束能同时成立**——这是本 change 的核心，论证分三步：

1. **「不编码 revision」约束的本体是 VRN 字符串，不是整个引用**。既有 VRN 规定是「URI/地址**字符串本身**不得携带 revision/hash/snapshot 引用」（`virtual_resources/AGENTS.md`「不得在 URI 中编码 revision/hash/snapshot ref」；在途 spec `context-injection-lifecycle/spec.md:331`「revision/hash/snapshot reference 不得编码进 URI」）。把 revision 放到**与 VRN 并列的结构化字段**，VRN 字符串逐字节仍不含 revision —— 约束按字面与按意图都未被破。旧方案之所以违约，恰恰是因为它把 `assembly={id}` 塞进同一个串。
2. **「可重读的修订绑定 locator」的能力本体是「能在固定修订上重读」，而不是「revision 出现在地址里」**。当前方案的 revision 其实已经**不在** `base` 里，而是在 `#selector` 与 base64 `cursor` 里（`SessionContextCursorCodec.encode` 把 `revision` 编进游标 payload）。把 revision 从 selector/cursor 提到一层显式字段后：
   - `expected_revision` 校验语义不变（不匹配 → 显式修订变更错误）；
   - 游标继续绑定 `resource + revision + operation`，分页继续在固定修订上推进；
   - 差别仅在**表示位置**（字符串内 → 结构化字段），能力不减。
3. **三层分离让「修订」有正确归宿**。revision 属于「资源在某一时刻的内容版本」，既不是 identity（identity 必须 revision-free），也不是 address（address 必须 revision-free）。**给它一个独立字段，正是三层分离的直接推论**，而不是为了绕过约束的妥协。

**被否决的替代方案**：

- *(a) 把 revision 塞进 VRN 的 path segment 或 query*：直接违反 D1 第 1 步的明文约束；query 也被 grammar 拒绝。否决。
- *(b) 保留 fragment 承载 revision/视图*：统一后 VRN 拒绝 `#`，保留 fragment 等于保留第二套语法。否决。
- *(c) 只用 base64 游标承载一切*（不暴露结构化字段）：调用方无法在**首次**请求声明期望 revision（游标通常由上一次响应产生），丢失「可重读声明」，且不可读、不可审计。否决。
- *(d) 把 revision 编进一个「带修订的 VRN」新 scheme*：等于再造一套并行语法，与本次统一目标背道而驰。否决。

### D2：与 `#fragment` 的关系——统一后不允许 fragment

**决定**：统一后的 VRN **不允许** `#fragment`（沿用资源平台既有 `fragment_rejected` 拒绝码，本 change 不新增码）。原 fragment 承载的信息迁移如下：

| 旧形态 | 承载信息 | 迁移目标（结构化字段） |
|---|---|---|
| `#information` | 视图选择 | `view = information` |
| `#record={index}` | 视图选择 + 记录定位 | `view = records` + `record_index = {index}`（或等价结构化选择） |
| `#assembly={assembly_id}` | 视图选择 + assembly 身份 | `view = assembly` + `assembly_ref`（以资源身份表达，不以字符串拼进地址） |

`view` 与资源种类的兼容性沿用既有校验语义（旧 `validate_session_context_read_view` 的规则集），并额外要求**未识别的 view 取值显式失败**，不得降级为默认视图。

**理由**：fragment 是把「位置」与「选择」拼进一个字符串的旧机制；统一后选择必须走结构化字段，否则就是在 VRN 上开一个语法后门，重新制造双轨。

### D3：scope 与必填 `scope_id` 的处理

**决定**：会话上下文资源使用权威表的 scope 闭集；权威形态为 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...}`。**每个 scope 的 `scope_id` 段一律必填**，本 change MUST NOT 把 `scope_id` 改为可选、MUST NOT 简化段序、MUST NOT 省略 `resources` 固定段。

旧形态里 `boxteam://session/{session_id}`（不带 scope_id）等价于「按当前工作区隐式解析」，统一后 MUST 显式化为带 scope_id 的规范形态，或由软件在**入口**规范化后立即冻结为显式形态。「当前工作区」MUST NOT 成为持久化数据的隐含前提。

**为什么是「保留既有段序」而不是简化段序**：简化版会把 `scope_id` 改成可选（旧模板曾如此），那等于对非 workspace 的 scope **重新引入隐含上下文**——而这正是本次改造要根除的东西。`scope_id` 全部必填，就是把「workspace_id 必须显式」这条原则扩展到所有 scope；这不是「改动更小」的妥协，而是原则上更对。

**注意（权威表与现状的差距）**：资源平台现有 grammar 的 scope 闭集为 `{workspace, gateway, builtin, memory}`，且**没有 `resources` 固定段**（现为 `boxteam://{scope}/{id}/{kind}/...`）。契约 v2 要求 scope 闭集扩为 `{workspace, user, gateway, inline, memory}`（`builtin` 正名为 `inline`）并保留 `resources` 固定段。本 change **不自行改动**该闭集或段序——VRN 语法本体归「统一虚拟资源寻址」change。本 change 只声明会话上下文侧遵循 v2 形态，并把闭集/段序/scope_id 表的落地留给该 change 统一登记。这是契约与现状的差距点（见 Open Questions）。

### D4：星型解析与会话上下文的接入

**决定**：会话上下文解析 MUST 走统一星型顺序：

1. 本地 parse（fail-closed，拒绝 `%`/`#`/非法 scope/非法 kind/malformed path）；
2. 本进程解析（本机资源）；
3. 本地 gateway **是自身联邦的 hub** 时，可直接解析其**直接 spoke** 的资源；
4. 本地 gateway **是 spoke** 时，通过其**唯一 hub** 做**一次有界 transit 解析**，携带 `visited set`、`max_transit_gateways=1`、`max_gateway_hops=2` 与总 deadline；
5. 不可达/未共享/未找到：fail-closed 结构化拒绝码，绝不猜测路径或返回默认值。

跨边界传输只含**资源身份、VRN、revision、内容**；`real path` 不跨边界。

**「locator 是输入，不是输出」不变量**：解析命中**只返回稳定身份与内容**，MUST NOT 返回或携带 locator。这是可机械检查的约束——可以写断言扫描所有跨边界响应，确认输出中不含 real path、provider locator 或任何解析 locator 形式。它与三层分离（real path 不跨边界）互相加固：输入侧用 VRN 声明位置，输出侧只回身份与内容。

**上界必须策略化**：`max_transit_gateways` / `max_gateway_hops` / deadline 等上界 MUST 表达为**显式策略常量**，MUST NOT 散落成魔法数字；拓扑变化时改策略而非重写解析器。

**理由**：与既有 federation/跨 Session 目标解析的有界拓扑（`B → A → C`、最多一个 hub transit）天然兼容；复用同一份 VRN 避免为会话上下文单开一条解析路径。

### D6：配置来源真实路径持久化的迁移（已确证义务）

**决定**：把配置来源的真实路径持久化改造为 VRN 表达，**直接复用 config 侧已跑通的形态**。

现状（已实测）：`app/services/infrastructure/config/state.py` 的 `ConfigSourceLayerRecord` 含 `source_path: str` 与 `backup_path: str | None`——真实路径已写入 SQLite；`app/schemas/internal_v2/config.py` 的 `ConfigSourceDTO.path: str` 又把它经 API 响应体对外。两处都违反「real path 永不持久化 / 永不进模型可见载荷」。

改造形态：`app/core/config_sources.py` 的 `ConfigSource` 已是 `path: Path` + `layer` + `precedence` 平级属性，且 `layer_revision` / `layer_digest` / `source_generation` 已是**兄弟字段**。因此改造等价于**把 `path: Path` 换成 `vrn: VRN`，其余兄弟字段原样保留**。MUST NOT 另发明一套结构。

**理由**：这是 v2 明示的迁移义务，属「已确证」而非假设。复用既有 sibling 字段形态可以同时达成两点——真实路径不再持久化、不再进 API 响应体；且不引入第三套来源层结构（避免双轨）。

### D5：命名与 owner 收口

**决定**：本 change 严格使用契约 v1 的逐字命名（见 Context）。在途 change `add-itemized-rollout-context` 的会话上下文 URI requirement（`specs/itemized-rollout-context/spec.md:262/278/288` 及 `design.md:890-967`、`tasks.md:126`）MUST 改为**引用本 change**，不再自行定义会话上下文 URI 语法，从而消除两套定义并存。

**理由**：一个语法只能有一个定义源；在途 change 尚未归档，可安全改指向。

## Risks / Trade-offs

- **[破坏性迁移会打断已持久化记录]** → 提供显式一次性迁移：可规范化者迁到 identity+VRN 并表示并保留 lineage；不可规范化者显式标记失效并报错；迁移前原始记录保留至整体确认成功，回滚恢复到等价只读状态。
- **[scope 闭集/段序与资源平台现有 grammar 不一致]** → 本 change 不自行扩张 VRN 闭集或改段序，只声明会话上下文侧遵循 v2 形态并要求「统一虚拟寻址」change 集中登记；在权威表与 grammar 对齐前，接口 MUST fail-closed，禁止临时映射悄悄上线。
- **[配置来源真实路径迁移影响既有 API 响应体字段]** → `ConfigSourceDTO.path` 是既有对外字段，改为 VRN 属**破坏性**变更；按 v2 允许破坏性迁移，落地时同步改 schema 与前端消费点，并在迁移计划中保留回滚边界。
- **[调用方习惯把 revision 拼进地址]** → 入口 MUST 显式拒绝旧式 fragment/`%` 形态，并在错误信息中指向结构化字段，避免调用方误以为「换个分隔符还能拼」。
- **[结构化字段被写成持久化事实的第三份拷贝]** → requirement 明确三层的唯一 owner（D1），新增字段一律 identity + VRN + 独立 revision，禁止存 real path。
- **[star-topology 引入对端信任边界]** → 沿用既有「对端本地解析并只回内容」的强制约定：本机不代对端解析 locator，对端不泄露 real path。

## Migration Plan

1. **冻结入口**：新增/切换会话上下文入口时，先按 D1 的结构化引用表示；旧式字符串入口对**新写入**直接拒绝（`%`/`#`/未登记 scope）。
2. **扫描既有持久化记录**：识别内嵌旧式上下文 URI 的 cursor、审计、引用字段；逐条判定可规范化 / 不可规范化。
3. **迁移可规范化记录**：解析旧记录 → 提取资源身份 + 位置 → 生成带必填 scope_id 的规范 VRN → revision/视图/游标落到结构化字段 → 保留来源 lineage。
4. **失效不可规范化记录**：显式标记失效并记录原因，**不得**静默丢弃或猜测。
5. **整体确认**：全部记录迁移成功且校验通过后，才允许清理旧表示；确认前原始记录保留。
6. **回滚边界**：任一步失败即停止并回滚——旧记录仍是权威，系统行为与迁移前等价；已迁移记录的回滚按 lineage 反向恢复。
7. **配置来源真实路径迁移（已确证义务，见 D6）**：把 `ConfigSource.path: Path` 换成 `vrn: VRN`，兄弟字段（`layer`/`precedence`/`layer_revision`/`layer_digest`/`source_generation`）原样保留；同步移除 `ConfigSourceLayerRecord.source_path`/`backup_path` 与 `ConfigSourceDTO.path` 对真实路径的持久化/输出。
8. **删除 bundled 到 builtin 的改名 shim**：落地时移除 `app/agents/skill_runtime.py:538` 的 bundled/builtin 映射，向 `inline` 正名收敛。

**部署顺序约束**：本 change 的 spec/design/tasks 先于「统一虚拟资源寻址」的 VRN 语法与拒绝码登记落地之前**不得**进入实施，因为会话上下文解析直接依赖其 grammar 与拒绝码。

## Open Questions

- **权威表下发前不得定稿**：scope 名、scope_id 语义、拒绝码三类内容必须以「统一虚拟资源寻址」change 随后下发的**权威表**为准。权威表到达前，本 change 的接口 MUST fail-closed，不得上线临时映射。
- **段序与闭集落地**：v2 要求保留 `resources` 固定段并把 scope 闭集扩为 `{workspace, user, gateway, inline, memory}`（builtin 正名为 inline）；资源平台现有 grammar 尚无 `resources` 段且闭集为 `{workspace, gateway, builtin, memory}`。该对齐由寻址 change 完成，本 change 不自行改动。
- **`memory` 归属冻结**：`memory` 未经本次重新定义，本 change 不基于它做设计、不为其定 scope_id；其归属等权威表。
- **拒绝码具体取值**：会话上下文新增拒绝场景（如「旧式 fragment 形态」「view 与资源不兼容」）落到哪个既有拒绝码或由寻址 change 登记的新码，待寻址 change 集中登记后引用。
- **`assembly_ref` 的表示**：D2 中 `assembly={id}` 迁为 `assembly_ref`，其具体采用资源身份还是专用 ref 类型，待与 itemized rollout context 的 assembly 身份模型对齐后确定（不改变本 change 的结构化方向）。
- **待验证项（不得在 spec 断言）**：(1) VRN 是否已落进持久化的 session/catalog/checkpoint 数据（决定迁移是新写字段还是真数据迁移）；(2) `ConfigSource.path` 是否经**间接路径**进 API 响应体（已确证 `ConfigSourceDTO.path` 为直接路径，间接路径待查）；(3) `inline`/`sqlite` 两层是否有可解析载体（不得照抄「inline 挂 gateway、sqlite 挂 workspace」这类推测）；(4) `memory` 的真实形态与归属。
