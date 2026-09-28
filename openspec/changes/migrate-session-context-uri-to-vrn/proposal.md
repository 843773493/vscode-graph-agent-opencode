## Why

会话上下文资源（`read_context` / `search_context` 与跨 Session 目标）目前使用一套**自有正则**（`app/services/business/session_context_resource.py` 的 `_SESSION_RESOURCE` 等），把「资源位置 + 视图选择 + 修订绑定」全部编进**同一个字符串**：`boxteam://workspace/{ws}/session/{session_id}#assembly={id}`。这套语法与资源平台既有的**虚拟资源地址 / VRN**（`resource_platform/virtual_resources/`）在实测中**双向 100% 互斥**——VRN 拒绝全部上下文 URI（`session` 不是已登记 scope；`#` 触发 `fragment_rejected`）；上下文 parser 也拒绝全部 VRN 地址。于是同一个 `boxteam://` scheme 下并存两套并行语法，新增一种资源引用时扩展点分裂、拒绝码各写一套、跨 Gateway 星型解析无法共用。

现在需要统一：让会话上下文从「两套并行语法」变成「**一套资源语法 + 一层修订绑定**」，同时不破坏 VRN 既有的「URI 不编码 revision/hash、不是 identity/capability/dedupe key」约束，也不丢失上下文侧「可重读修订 locator」的能力。

## What Changes

- **BREAKING**：废弃会话上下文的自有 URI 正则与 `#fragment` 语法。资源位置一律改用**虚拟资源地址 / VRN**（由「统一虚拟资源寻址」change 拥有的唯一规范化实现），`revision`、视图选择、分页游标改为**与 VRN 并列的结构化兄弟字段**，不再编码进地址字符串。
- **BREAKING**：`boxteam://session/{session_id}#assembly={id}`、`#record={n}`、`#information` 等形态**全部失效**；原先由 fragment 承载的信息迁移到结构化字段（见 design「与 `#fragment` 的关系」）。
- 新增/统一**资源身份 / ResourceIdentity** 与**虚拟资源地址 / VRN** 的三层分离：资源身份不透明、稳定、不含 revision、不依赖当前激活工作区；VRN 可解析、允许悬空、禁止编码 revision/hash；**真实路径 / real path** 只在最后访问点出现，永不持久化、永不进模型可见载荷、永不跨 gateway 边界。
- 统一 **scope（闭合 3 值）**为 `workspace` / `user` / `gateway`；`workspace` 路径必须显式带 `workspace_id`，「当前工作区」不再作为寻址概念的隐含前提。
- 采用**星型解析 / star-topology resolution**：本地 parse(fail-closed) → 本进程解析 → authority 对端交 gateway 层并按同一份 VRN 在对端本地解析、只回内容 → 不可达/未共享/未找到一律 fail-closed **拒绝码 / rejection code**。
- **迁移既有已持久化的上下文 URI 字符串**：给出规范化迁移与失效判定、回滚边界（见 design）。
- 收口在途 change `add-itemized-rollout-context`：其 `specs/itemized-rollout-context/spec.md` 的会话上下文 URI requirement 指向本 change，消除两套定义并存。

## Capabilities

### New Capabilities
- `session-context-resource-addressing`: 会话上下文资源的寻址与解析合同——以 VRN 表达资源位置、以结构化兄弟字段承载 revision/视图/游标、三层分离不变量、scope 与 gateway authority 解析顺序、拒绝码引用与 fail-closed 行为、破坏性迁移与失效判定。

### Modified Capabilities
<!-- 无：被收口的在途 change 尚未归档，其 delta 归它自己；已发布的 openspec/specs/** 对会话上下文 URI 无 requirement（实测 0 命中），故不产生 modified capability。 -->

## Impact

- **规划产物**：新增本 change 的 `proposal.md` / `specs/session-context-resource-addressing/spec.md` / `design.md` / `tasks.md`；更新 `openspec/changes/add-itemized-rollout-context/` 的 URI 相关 requirement/task 指向。
- **受影响系统（实施阶段，不在本 change 落地）**：会话上下文查询服务与其资源解析器、`read_context` / `search_context` 工具 schema、跨 Session 目标（`send_message_to_session` / `wait_for_session`）的地址入参、已持久化的 cursor/审计记录中可能内嵌的旧 URI 字符串、Gateway 星型路由的会话资源分支。
- **依赖**：本 change **不拥有** VRN 语法本体与拒绝码登记——由「统一虚拟资源寻址」change 独占；本 change 只引用其 grammar 与拒绝码命名空间。
- **不做**：不引入 `virtual url` / `VURI` 等同义异名；不把 revision/hash 编码进 VRN；不以「当前工作区」作为持久化数据的隐含前提。
