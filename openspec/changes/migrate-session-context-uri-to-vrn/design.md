## Context

见 `proposal.md` 的 Why。此处只补充塑造方案所需的现状与约束。

当前会话上下文资源的真实形态（`app/services/business/session_context_resource.py`）：

- 三个自有正则 `_SESSION_RESOURCE` / `_WORKSPACE_SESSION_RESOURCE` / `_WORKSPACE_SESSIONS_RESOURCE` 各自 `fullmatch` 一个字符串；
- `parse_session_context_resource(resource)` 先按 `#` 切出 `base` 与 `selector`，selector 只允许 `information` / `assembly={id}` / `record={index}` 三种；
- `ParsedSessionContextResource` 字段为 `(canonical, base, kind, session_id, workspace_id, selector)`；
- `SessionContextCursorCodec` 把 `{resource, revision, operation, offset, char_offset}` 做 base64 游标，`decode` 时校验 `revision` 与 `resource`/`operation` 一致；
- `expected_revision` 已作为**独立参数**存在（`ReadContextInput.expected_revision`），`require_session_context_revision` 在不匹配时抛显式修订变更错误。

与此并存的资源平台 VRN（`resource_platform/virtual_resources/`）实测形态：

- `parse_vrn` 只接受 `boxteam://{scope}/...`，scope 闭集（实测现状）`{workspace, user, gateway, inline}`（`memory` 已由提交 `32bc6256` 物理移除；`builtin` 正名 `inline` 与 `user` 新增均已由 `298ef599`+`f6fc990f` 落地），`kind` 闭集（实测现状）`{agent-spec, skills, config, session}`；
- 整体拒绝 `%`（`percent_encoding_rejected`）与 `#`（`fragment_rejected`）；
- 双向交叉解析**实测 100% 互斥**：VRN 拒绝全部 6 条上下文 URI；上下文 parser 拒绝全部 3 条 VRN 地址。

因此冲突的**技术根因是两种信息被塞进同一个字符串**：上下文侧把「资源位置」与「修订/视图绑定」编在一起，而 VRN 明确只承担前者。

### 约束（权威表已下发，本轮定稿）

- VRN **禁止编码 revision/hash**；identity 独立于 VRN；`real path` 永不持久化 / 永不进模型可见载荷 / 永不跨 gateway。
- 权威 VRN 形态（保留既有段序，**不得简化**）：`boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}`。`resources` 固定段保留；`scope_id` 对**所有** scope 都必填。
- scope 闭合集定稿为 `workspace` / `user` / `gateway` / `inline`：`builtin` 正名为 `inline`，`user` 为本次新增；`memory` **已确证不是 VRN scope，移出闭合集**（零生产构造方、resolver 不比对 scope_id、container 未装配、configs 自述未接入）。
- `scope_id` 必须由真实身份推导、MUST NOT 硬编码字面量：`workspace`→真实 workspace_id、`gateway`→真实 gateway_id（取值来源与注入 owner 按「统一虚拟资源寻址」change 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」定稿，本 change 只具名引用、不复述取值规则）、`inline`→真实 distribution_id、`user`→`local`（单用户本地约定，已由 owner 定为终值）。
- 星型解析唯一顺序，上界为显式策略常量；不可达/未共享/未找到 fail-closed 结构化拒绝码；**locator 是输入不是输出**。
- 拒绝码**本 change 只引用其中两套**——`grammar.py` 的 17 个与 `resolver.py` 的 6 个——由「统一虚拟资源寻址」change 集中登记（该 owner 另登记**第三套：联邦解析期** `FederationError.code`，归属 `app/gateway/federation/`，见其 requirement「拒绝码必须分三套集中登记且命名不得自造」，本 change 不引用该套、也不实现跨 gateway 解析）；本 change **只能引用不能自造**，且 MUST NOT 混用任何两套闭集。VRN 语法本体、固定段序与 `kind` 闭集同样不由本 change 拥有。
- `memory` 不是 VRN scope：不基于它做设计、不为它规定 scope_id；既有两点式 `boxteam://memory/{scope}/{name}` 只作**非 VRN 示意**（它无 `resources` 段、无 kind、恰好两段，曾走独立特例分支；**该特例分支已由提交 32bc6256 物理删除，现以 `unknown_scope` 类拒绝码 fail-closed 拒绝**）。
- 命名必须逐字使用：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`。

## Goals / Non-Goals

**Goals:**

- 把会话上下文从「两套并行语法」变成「一套资源语法 + 一层修订绑定」。
- 论证并落地「VRN 不编码 revision」与「上下文可重读修订 locator」**同时成立**。
- 给出入口破坏性拒绝与「新写字段」迁移面，说明为何不需要历史数据迁移。
- 收口在途 change `add-itemized-rollout-context` 的会话上下文 URI requirement，消除两套定义并存。

**Non-Goals:**

- 不定义 VRN 语法本体、scope 关键字集合、`kind` 闭集与拒绝码登记（归「统一虚拟资源寻址」change）。
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

**决定**：会话上下文资源使用权威表的 scope 闭集 `workspace` / `user` / `gateway` / `inline`；权威形态为 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...}`。**每个 scope 的 `scope_id` 段一律必填**，本 change MUST NOT 把 `scope_id` 改为可选、MUST NOT 简化段序、MUST NOT 省略 `resources` 固定段。

旧形态里 `boxteam://session/{session_id}`（不带 scope_id）等价于「按当前工作区隐式解析」，统一后 MUST 显式化为带 scope_id 的规范形态，或由软件在**入口**规范化后立即冻结为显式形态。「当前工作区」MUST NOT 成为持久化数据的隐含前提。

**为什么是「保留既有段序」而不是简化段序**：简化版会把 `scope_id` 改成可选（旧模板曾如此），那等于对非 workspace 的 scope **重新引入隐含上下文**——而这正是本次改造要根除的东西。`scope_id` 全部必填，就是把「workspace_id 必须显式」这条原则扩展到所有 scope；这不是「改动更小」的妥协，而是原则上更对。

**`memory` 移出闭合集（权威表裁定）**：实测 `memory` 无资源、无生产调用方、无持久化载体；resolver 连 `scope_id` 都不比对，`kind` 为 `None`，container 未装配，configs 自述未接入。把 `boxteam://memory/{scope}/{name}` 当 VRN 会让它绕过 `resources` 固定段与 kind 校验，等于在统一语法上开一个特例后门。故它 MUST 只作**非 VRN 示意**，入口 MUST 以「未登记 scope」拒绝。

**`scope_id` 必须由真实身份推导**：`gateway` 现状在 skill 目录生成链路上把 `scope_id` 硬编码为字面量 `"local"`（`app/agents/skill_runtime.py:539` 的 `else "local"`），`inline` 的 `distribution_id` 现已按 `app/core/distribution_identity.py::load_distribution_id()` 从发行包 runtime manifest 推导（`298ef599`+`f3bd8213` 落地），不再是全仓零赋值、也不再与 `gateway` 共用字面量；`gateway` 的 `local` 字面量仍待请求级注入切片，属既有不一致的剩余部分。定稿表要求 `gateway`→真实 gateway_id、`inline`→真实 distribution_id，落地时按真实身份推导，不得继续共用字面量；`distribution_id` 的来源与编码已由「统一虚拟资源寻址」change 定稿为发行包 runtime manifest 的 `distribution` + `version`（见其 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」，本 change 只具名引用、不复述取值规则）；`gateway_id` 的来源与注入 owner 亦已由该 change 定稿（见其 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」，本 change 只具名引用、不复述取值规则）；`user`→`local` 为单用户本地程序约定。

**注意（权威表与现状的差距）**：资源平台 grammar 的 scope 闭集编写时曾为 `{workspace, gateway, builtin, memory}`（其中 `memory` 已由提交 32bc6256 物理移除），`kind` 闭集为 `{agent-spec, skills}`。现存 grammar 已含 `resources` 固定段（`boxteam://{scope}/{id}/resources/{kind}/...`）。权威表要求的闭集 `{workspace, user, gateway, inline}` 已随 `298ef599`/`f6fc990f` 落地，`resources` 固定段已保留。本 change **不自行改动**该闭集、`kind` 闭集或段序——VRN 语法本体归「统一虚拟资源寻址」change。本 change 只声明会话上下文侧遵循该形态，并把闭集/段序/scope_id 表的落地留给该 change 统一登记。

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

### D5：迁移面是「新写字段」而非「存量数据迁移」（U1 实测定稿）

**决定**：本次寻址统一的迁移工作**不含历史数据迁移**，只含两点——(a) 入口对旧形态 fail-closed 拒绝；(b) 既有持久化字段改为按新格式**新写入**并在读路径切换。

**已确证的零存量（本轮独立取证）**：旧式会话上下文 URI 没有任何持久化实例。

- 旧形态字符串全部在请求/响应链路内构造并随响应返回调用方：`app/services/business/session_context_resource.py:12-16` 的自有正则只做解析、`ParsedSessionContextResource.canonical` 只是内存值、`session_context_query_service.py`/`session_context_projection.py` 的 `locator` 与 base64 `next_cursor` 只进 API DTO（`app/schemas/internal_v2/session_context.py:99,123`）。全仓不存在把 `SessionContextCursorCodec` 输出写入 session/catalog/checkpoint/rollout 存储的路径。
- 全仓唯一承载 `boxteam://` 的持久化列是 `resource_activation_bindings.display_uri`（`resource_activation_schema.py:85`）；其唯一写入方 `ResourceActivationStore.persist_snapshot` 的**调用方全在测试**（`tests/unit/services/infrastructure/rollout_context/test_resource_activation_storage.py`、`test_resource_activation_retention.py`、`test_resource_activation_fork_identity.py`），`app/container.py` 未装配（实测 `rg resource_activation app/container.py` 退出 1），生产 seal 链路从未传入 `activation_snapshot`（恒为默认 `None`），故生产中该列从不被写入。
- 磁盘取证：157 个 live 库 + 44 个 dev/temp 库中 **0 个 activation 表、0 个 `boxteam://` 命中**；`out/development-runtime` 的真实 `rollout.jsonl`（含 2026/09/24、2026/09/27 会话）与 `tests/fixtures/` 亦 0 命中。
- 唯一 `cursor` 持久化列 `workspace_event_cursors.cursor_value`（`app/services/infrastructure/workspace_state_store.py:71`）承载的是工作区活动 `event_seq`，与上下文游标无关。

**新写字段的挂点**（既已存在、只是尚未承载 VRN）：`context_source_control_states`（`runtime/context_sources/registry.py`）已持久化来源追踪事实（`source_id`/`name`/`revision`）但无 URI 列；`resource_activation_bindings.display_uri` 已建表但生产从不写。落地方向是让它们按 identity + VRN 的新格式写入。

**理由**：不构造不存在的迁移脚本，既省成本，也避免「为一个不存在的存量对象写规范化/失效逻辑」这种虚假工作。

### D6：配置来源真实路径持久化的迁移（已确证义务）

**决定**：把配置来源的真实路径持久化改造为 VRN 表达，**直接复用 config 侧已跑通的形态**。

现状（已实测）：`app/services/infrastructure/config/state.py` 的 `ConfigSourceLayerRecord` 含 `source_path: str` 与 `backup_path: str | None`——真实路径已写入 SQLite；`app/schemas/internal_v2/config.py` 的 `ConfigSourceDTO.path: str` 又把它经 API 响应体对外（`app/api/config.py:102` 的 `path=str(source.path)`，实测 `GET /api/v1/config/sources` 回真实绝对路径）。两处都违反「real path 永不持久化 / 永不进模型可见载荷」。

改造形态：`app/core/config_sources.py` 的 `ConfigSource` 已是 `path: Path` + `layer` + `precedence` 平级属性，且 `layer_revision` / `layer_digest` / `source_generation` 已是**兄弟字段**。因此改造等价于**把 `path: Path` 换成 `vrn: VRN`，其余兄弟字段原样保留**。MUST NOT 另发明一套结构。

**config 寻址形态**：config 资源用「统一虚拟资源寻址」change 已定稿的 `config` kind 标识来源文件本身（见其 requirement「kind 闭集定稿且描述符闭集独立不可混用」与「配置来源寻址必须使用 config kind 且 sqlite 层不可寻址」）；`layer` 作为**兄弟字段**保留，**不塞进 VRN**。`inline` 层有稳定 disk 载体（发行包内 `configs/*_inline.jsonc`，经 `resolve_config_resource_source` 校验 `is_file()`），故有 VRN。`sqlite` 层 MUST NOT 编 VRN：`user` / `user_local` / `workspace` 三层共享同一个 `workspace.sqlite`（`app/services/infrastructure/config_service.py` 的 `_config_source` 在 `_workspace_state_store` 存在时统一返回 `self._workspace_state_store.path`），单一 VRN 会立刻对应多个逻辑来源。

**理由**：这是明示的迁移义务，属「已确证」而非假设。复用既有 sibling 字段形态可以同时达成两点——真实路径不再持久化、不再进 API 响应体；且不引入第三套来源层结构（避免双轨）。

### D7：命名与 owner 收口

**决定**：本 change 严格使用契约的逐字命名（见 Context）。在途 change `add-itemized-rollout-context` 的会话上下文 URI requirement（`specs/itemized-rollout-context/spec.md:262/278/288` 及 `design.md:890-967`、`tasks.md:126`）MUST 改为**引用本 change**，不再自行定义会话上下文 URI 语法，从而消除两套定义并存。

**理由**：一个语法只能有一个定义源；在途 change 尚未归档，可安全改指向。

## Risks / Trade-offs

- **[入口破坏性拒绝会打断调用方]** → 入口对旧式 fragment/`%` 形态显式拒绝并在错误信息中指向结构化字段；因已确证无历史落盘实例，不存在需要保持可读的旧记录。
- **[scope 闭集/段序与资源平台现有 grammar 不一致]** → 本 change 不自行扩张 VRN 闭集或改段序，只声明会话上下文侧遵循权威形态并要求「统一虚拟寻址」change 集中登记；在 grammar 与权威表对齐前，接口 MUST fail-closed，禁止临时映射悄悄上线。
- **[配置来源真实路径迁移影响既有 API 响应体字段]** → `ConfigSourceDTO.path` 是既有对外字段，改为 VRN 属**破坏性**变更；按允许破坏性迁移处理，落地时同步改 schema 与前端消费点，并在迁移计划中保留回滚边界。注意这是单用户本地程序，实际安全影响低，主要属契约卫生，故按常规迁移任务处理，不单独开紧急修复。
- **[结构化字段被写成持久化事实的第三份拷贝]** → requirement 明确三层的唯一 owner（D1），持久化字段一律 identity + VRN + 独立 revision，禁止存 real path。
- **[star-topology 引入对端信任边界]** → 沿用既有「对端本地解析并只回内容」的强制约定：本机不代对端解析 locator，对端不泄露 real path。
- **[`scope_id` 从硬编码字面量转为真实身份推导会改变既有字符串]** → `gateway`/`inline` 现共用字面量 `"local"`，改动会产生不同的 VRN 字符串；因这些字符串当前只进内存 registry 与响应、不落盘（见 D5），属契约级调整而非数据迁移。

## Migration Plan

1. **冻结入口**：会话上下文入口按 D1 的结构化引用表示；旧式字符串入口对**新写入**直接拒绝（`%`/`#`/未登记 scope，含 `memory` 两点式）。
2. **确认无存量**：按 D5 的取证确认不存在内嵌旧式上下文 URI 的持久化记录，**不构造**扫描/规范化/失效的存量迁移脚本。
3. **新写字段切换**：让既有持久化挂点（`display_uri` 列、`context_source_control_states` 的来源事实）按 identity + VRN 的新格式写入，并在读路径切换到新格式，旧写入形态物理下线。
4. **配置来源真实路径迁移（已确证义务，见 D6）**：把 `ConfigSource.path: Path` 换成 `vrn: VRN`，兄弟字段（`layer`/`precedence`/`layer_revision`/`layer_digest`/`source_generation`）原样保留；同步移除 `ConfigSourceLayerRecord.source_path`/`backup_path` 与 `ConfigSourceDTO.path` 对真实路径的持久化/输出。
5. **删除 bundled 到 builtin 的改名 shim**：落地时移除 `app/agents/skill_runtime.py:538` 的 `bundled`→`builtin` 映射并同步 `inline` 正名；注意 `layer` 名（`bundled`）进入 `entry_identity` 与 catalog payload，属契约级变更，需评估同步面而非纯改名。

**部署顺序约束**：本 change 的 spec/design/tasks 先于「统一虚拟资源寻址」的 VRN grammar 与拒绝码登记落地之前**不得**进入实施，因为会话上下文解析直接依赖其 grammar 与拒绝码；`kind` 取值（`session`）已由该 change 定稿，不再是前置阻塞项。

## Open Questions

- **会话上下文资源自身的 `kind`（已定稿，不再是 open question）**：由「统一虚拟资源寻址」change 在 kind 闭集内定稿为 `session`（其 requirement「kind 闭集定稿且描述符闭集独立不可混用」，闭集为 `agent-spec` | `skills` | `config` | `session`）。本 change 直接引用该已登记取值，无需新登记、无待裁定项；规范形态为 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/session/{...canonical path segments}`。`scope_id` 语义同样已由该 owner 定稿，本 change 直接引用。
- **拒绝码的具体归属**：会话上下文新增拒绝场景（如「旧式 fragment 形态」「view 与资源不兼容」「memory 两点式」）落到 `grammar.py` 的 17 个码还是 `resolver.py` 的 6 个码，由寻址 change 集中登记后引用；本 change 只引用 grammar/resolve 这两套、不新增码、不混用闭集，第三套（联邦解析期）归属见 `add-unified-virtual-resource-addressing` 的拒绝码登记 requirement「拒绝码必须分三套集中登记且命名不得自造」。
- **`assembly_ref` 的表示**：D2 中 `assembly={id}` 迁为 `assembly_ref`，其具体采用资源身份还是专用 ref 类型，待与 itemized rollout context 的 assembly 身份模型对齐后确定（不改变本 change 的结构化方向）。
- **`user` scope 的未来扩展（不影响当前终值）**：`user` → `local` 已由「统一虚拟资源寻址」change 定为**终值**（其 requirement「scope 必须取自定稿闭集且 scope_id 对所有 scope 必填」规定 `user` → `local` 并 MUST 显式声明为单用户本地程序约定）。本 change 直接引用该终值，不存在后续判定。若将来出现多用户场景如何扩展语义（例如是否引入用户名细分），属**未来可能**，须由 owner 另行发起变更，不得据此改动当前终值。
