## Why

当前仓库的 `boxteam://` 地址存在两套并行、互不可解析的解析器：`app/services/infrastructure/resource_platform/virtual_resources/grammar.py` 的 VRN grammar（资源寻址）与 `app/services/business/session_context_resource.py` 的会话上下文正则（会话定位），且 VRN 的解析/授权侧零生产调用（审计结论：只有 `skill_display_uri` 一个字符串构造函数被 `app/agents/skill_runtime.py:541` 使用）。同时 skill 链路内部还残留两处绕过 owner 的裸 `boxteam://` 拼接（`skill_runtime.py:52`、`skill_runtime.py:619`），其实测被自家 grammar 以 `malformed_path` 拒绝。

用户已拍板统一寻址方向：`workspace` / `user` / `gateway` / 其它工作区 / 其它 gateway 收敛到同一套寻址；VRN 解析是顶层 gateway 之间的星型网络；identity 独立于 VRN；配置/skill/状态默认用虚拟地址传递，真实路径只在最后访问点出现。本 change 是这套寻址抽象、词汇与语法定义的**唯一 owner**，另两个并行 change（会话上下文 URI 统一改造、单后端多工作区挂载）只引用本 change 的术语与定义。

## What Changes

- **BREAKING** 以统一 VRN 语法 `boxteam://[{gateway_authority}]/{scope}/[{workspace_id}/]{kind}/{...canonical path segments}` 取代现有资源形态；现有 `boxteam://workspace/{ws}/resources/skills/{name}/SKILL.md` 是该语法的特例（authority 缺省），但旧解析实现与旧裸拼接形式一并下线，不留兼容层与别名。
- 建立**冻结契约 v1** 的三层职责分离并作为本 change 的核心不变量：`ResourceIdentity`（资源身份，不透明、稳定、无 revision、不依赖当前激活工作区、持久化）／`VRN`（虚拟资源地址，可解析、持久化、允许悬空、禁编码 revision/hash）／`real path`（真实路径，机器本地、临时、永不持久化、永不进模型可见载荷、永不跨 gateway 边界）。
- 收敛 **scope 闭合集为 3 个**：`workspace` | `user` | `gateway`。`workspace` 路径 MUST 显式携带 `workspace_id`；「当前工作区」不是寻址概念，不得作为持久化数据的隐含前提。「其它工作区」复用 `workspace` scope + 另一个 `workspace_id`；「其它 gateway」用可选 **gateway authority** 段表达（缺省 = 本机，== self = 等价本机，== 对端 = 跨 gateway）。
- 定义 **星型 gateway 解析链**唯一顺序：本地 parse（fail-closed）→ 无 authority 或 authority==self 时本进程按 workspace registry 解析 → authority==对端时交 gateway 层解析器转发，对端本地解析并以 identity/VRN/revision/内容应答 → 不可达/未共享/未找到一律 fail-closed 返回结构化拒绝码。绝不回退本地猜测路径、空路径或虚假默认值。
- **拒绝码集中登记**：复用既有 grammar 拒绝码命名空间与风格；仅在跨 gateway 确需时新增最少数量（`unknown_gateway` / `remote_unreachable` / `remote_not_shared`），且只在本 change 内登记一处，其它 change 只能引用。
- 固化 **identity 独立于 VRN**：同一逻辑名出现在 `user` 与 `workspace` 两个 scope 时是两个不同 identity；跨来源等价/覆盖是另一个 concern，不得塞进 identity 或 VRN。
- 固化**默认寻址政策**：软件内部一切配置/skill/状态/资源引用默认用 VRN 传递；新增持久化字段若需定位资源，一律用 `identity + VRN(+ 独立 revision 字段)`，禁止存 real path。
- 建立**可机械检查的 real path 不变量**：real path 只在调用栈内有效；real path 出现在 API 响应体／持久化记录／模型可见载荷中即为缺陷。
- **收口在途 change**：更新未归档的 `add-context-injection-lifecycle`，把其未完成的 VRN 接线任务指向本 change，并显式声明「VRN 语法以本 change 为准」，消除两套定义并存的可能。
- 命名一致性作为跨 change 硬约束：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`，禁止 virtual url / VURI / 虚拟 URL 混用。

## Capabilities

### New Capabilities

- `virtual-resource-addressing`: 统一虚拟资源寻址的三层职责分离（ResourceIdentity / VRN / real path）、3 个闭合 scope、统一 VRN 语法与规范化、可选 gateway authority、星型 gateway 解析链、闭合拒绝码登记、身份与寻址的职责边界，以及 real path 永不外泄的可检查不变量。

### Modified Capabilities

（无。本 change 建立新 capability；`add-context-injection-lifecycle` 的既有 VRN requirement 更新由该 change 自身的 delta 承接，采用「引用本 change」而非在本 change 内改它的 spec。）

## Impact

- 新增 spec：`openspec/specs/virtual-resource-addressing/spec.md`（经本 change 的 delta 建立）。
- 收口修改：`openspec/changes/add-context-injection-lifecycle/tasks.md`（接线任务改指向本 change）与其 `specs/context-injection-lifecycle/spec.md`（声明 VRN 语法以本 change 为准）。
- 实施期（本 change 不写生产代码，仅登记影响面）：`app/services/infrastructure/resource_platform/virtual_resources/`（语法/解析/值对象重构）、`app/agents/skill_runtime.py`（两处裸拼接回归 owner）、`app/services/business/session_context_resource.py`（由并行 change 负责改造，本 change 只承认其归属）、以及 gateway 联邦解析层（星型转发）。
- 与并行 change 的边界：会话上下文 URI 统一改造、单后端多工作区挂载均引用本 change 的术语/scope/拒绝码定义，不得各自重新定义；本 change 只负责在寻址层承认「一个后端可挂载多个工作区、workspace 身份必须显式」。
- 破坏性：现有 `boxteam://workspace/{ws}/resources/skills/...` 形态与其解析实现、以及 `skill_runtime.py:52` / `skill_runtime.py:619` 的裸拼接路径一并收敛，不提供旧形式别名、双读或兼容 adapter。

