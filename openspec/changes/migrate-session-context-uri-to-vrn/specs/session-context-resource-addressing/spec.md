## Purpose

定义会话上下文资源的统一寻址与解析合同：资源位置一律以虚拟资源地址（VRN）表达，修订、视图与游标作为并列的结构化字段，从而在同一套资源语法上同时满足「地址不编码 revision」与「可重读的修订绑定 locator」两个约束。

## ADDED Requirements

### Requirement: 资源引用必须遵循三层分离

**归属与引用**：三层分离（`资源身份 / ResourceIdentity` / `虚拟资源地址 / VRN` / `真实路径 / real path`）的正名 normative 定义见 `add-unified-virtual-resource-addressing` 的 requirement「三层职责必须严格分离」。本 capability 只**具名引用**该 requirement，MUST NOT 复述其正文、MUST NOT 另立第二套三层分离规范；以下 scenario 只保留会话上下文侧的验收视角。

#### Scenario: 持久化记录不得含 real path
- **WHEN** 任意会话上下文资源被写入持久化记录或返回给模型
- **THEN** 记录中只出现资源身份与 VRN（以及独立的 revision 字段），不出现任何真实路径、绝对路径或 provider locator

#### Scenario: 资源身份不随激活工作区变化
- **WHEN** 同一资源在切换激活工作区前后被引用
- **THEN** 其资源身份保持逐字节相同，且不因当前激活工作区改变而改变

### Requirement: scope 必须取自闭合集且每个 scope 的 scope_id 一律必填

系统 MUST 只承认闭合集内的 scope，MUST 拒绝任何其它 scope 取值。scope 闭集为 **`workspace` | `user` | `gateway` | `inline`**（依据权威表：`builtin` 正名为 `inline`；`user` 为本次新增）。scope 闭集与每个 scope 的 scope_id 取值来源 MUST 由「统一虚拟资源寻址」change 的**唯一权威表**规定；本 capability MUST NOT 自行发明 scope 名或 scope_id 语义。

**`memory` 已确证不是 VRN scope，MUST NOT 出现在闭合集内**：它零生产构造方、resolver 连 scope_id 都不比对、container 未装配、configs 自述未接入。既有两点式 `boxteam://memory/{scope}/{name}`（无 `resources` 固定段、无 kind、恰好两段）MUST 被显式标注为**非 VRN 示意**，MUST NOT 被当作合法 VRN 接受或产出（其解析期特例分支已由提交 32bc6256 物理删除，现以 `unknown_scope` 类拒绝码 fail-closed 拒绝）。

**每个 scope 的 `scope_id` 段一律必填**，MUST NOT 只对某个 scope 必填而对其它 scope 可选。`scope_id` MUST 由**真实身份推导**，MUST NOT 硬编码字面量，MUST NOT 依赖隐含上下文：

- `workspace` → 真实 workspace_id；
- `gateway` → 真实 gateway_id（现状在 skill 目录生成链路上硬编码字面量 `"local"`，落地时改为真实身份推导，MUST NOT 继续硬编码字面量）；取值来源与注入 owner MUST 按「统一虚拟资源寻址」change 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」定稿落地，本 capability 只引用、不复述取值规则；
- `inline` → 真实 distribution_id（`distribution_id` 现已按 `app/core/distribution_identity.py::load_distribution_id()` 从发行包 runtime manifest 推导，不再是全仓零赋值；`gateway` 的 `local` 字面量仍待请求级注入切片，属既有不一致的剩余部分）；来源与编码 MUST 按「统一虚拟资源寻址」change 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」从其发行包 runtime manifest 的 `distribution` + `version` 推导，本 capability 只引用、不复述取值规则；
- `user` → `local`，并 MUST 显式声明为单用户本地程序的约定。

「当前工作区」MUST NOT 作为寻址概念的隐含前提，也 MUST NOT 作为持久化数据的隐含前提。

「其它工作区」MUST 表达为同一 scope 加另一个 scope_id 取值，MUST NOT 引入新 scope；「其它 gateway」MUST 表达为可选的**网关授权段 / gateway authority**，其缺省值为本机。

#### Scenario: 拒绝未登记的 scope
- **WHEN** 调用方提交 scope 不属于闭合集的 VRN（例如 `memory`、`session`）
- **THEN** 解析在访问任何 provider 之前以结构化拒绝码显式失败，不读取文件、网络或内存资源

#### Scenario: 任何 scope 缺少 scope_id 必须失败
- **WHEN** 调用方提交的 VRN 在 scope 段之后缺少 scope_id 段（对任意 scope，而非仅某一种）
- **THEN** 解析显式失败，且不得回退到隐含上下文补全

#### Scenario: memory 两点式形态被拒绝
- **WHEN** 调用方提交 `boxteam://memory/{scope}/{name}` 一类两点式字符串
- **THEN** 系统以不明 scope 显式拒绝，不得按 VRN 解释该字符串

### Requirement: VRN 语法形态统一且由单一 owner 规范化

会话上下文资源 MUST 使用统一 VRN 形态（保留既有段序，MUST NOT 简化）：

`boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}`

- `{gateway_authority?}`：可选、**单段**，承载稳定 gateway_id；缺省 = 本机 gateway。**owner 待实施项（关联 `add-unified-virtual-resource-addressing` task 3.4）**：该段在 `grammar.py` 的 `parse_vrn` 中当前无解析分支（authority 首段会被当作 scope 并以 `unknown_scope` 拒绝），本 capability 对 authority 的使用依赖 `add-unified-virtual-resource-addressing` 的 authority 实现。
- `{scope}`：必填，取自闭合集。
- `{scope_id}`：必填，**对所有 scope 都必填**。
- `resources`：固定段，MUST 保留。
- `{kind}`：必填，取自闭合集。
- `{...canonical path segments}`：canonical 尾段。

VRN 的字符集 MUST 为闭合集合；解析 MUST 拒绝 `%` 百分号编码与 `#fragment`；规范化 MUST 由**单一实现**完成。

本 capability MUST NOT 定义 VRN 语法本体，MUST NOT 自行变更固定段序，MUST NOT 把 `scope_id` 改为可选，也 MUST NOT 新增拒绝码；VRN grammar、固定段序、kind 闭集与**拒绝码 / rejection code** 登记由「统一虚拟资源寻址」change 独占，本 capability 只引用。

#### Scenario: 拒绝百分号编码与 fragment
- **WHEN** 调用方提交含 `%` 编码或 `#fragment` 的 VRN
- **THEN** 解析以既有 grammar 的对应拒绝码显式失败，且不产生二次解码歧义

#### Scenario: 规范化只走单一实现
- **WHEN** 同一逻辑资源经不同调用入口被寻址
- **THEN** 规范化结果逐字节一致，且由同一份实现产出

#### Scenario: 固定段序不得被简化
- **WHEN** 系统构造任何会话上下文资源的 VRN
- **THEN** VRN 保留 `resources` 固定段且 `scope_id` 段必填，不得省略二者或调换段序

### Requirement: 会话上下文以 VRN 表达位置并用结构化兄弟字段承载修订

会话上下文资源的位置 MUST 只由 VRN 表达。`revision`、视图选择与分页游标 MUST 作为与 VRN **并列的结构化字段**传递，MUST NOT 被编码进 VRN 字符串。

系统 MUST 同时满足：(a) VRN 不编码 revision/hash；(b) 调用方可获得**可重读的修订绑定 locator**，即同一请求能声明期望 revision 并在不匹配时显式失败、且分页可在固定修订上继续。

#### Scenario: 修订不进入 VRN
- **WHEN** 调用方按指定 revision 请求某会话上下文资源
- **THEN** 生成的 VRN 字符串在任何情况下都不包含该 revision 或任何 hash/snapshot 引用

#### Scenario: 修订不匹配必须显式失败
- **WHEN** 调用方声明的期望 revision 与目标资源当前 revision 不一致
- **THEN** 系统返回显式的修订变更错误，且不得静默返回新修订内容

#### Scenario: 分页在固定修订上继续
- **WHEN** 调用方携带游标继续读取同一资源
- **THEN** 系统校验游标绑定的资源与修订后在同一修订上继续，游标与当前资源或修订不匹配时显式失败

### Requirement: 会话上下文视图选择必须结构化

会话上下文的视图选择（原 `#information`、`#record={index}`、`#assembly={assembly_id}` 等 fragment 承载的信息）MUST 迁移为结构化字段，且 MUST NOT 再使用 `#fragment`。

系统 MUST 对「视图字段」与「资源种类」的组合做显式校验，拒绝不兼容组合，且 MUST NOT 静默忽略未识别的视图取值。

#### Scenario: 原 fragment 信息可经结构化字段表达
- **WHEN** 调用方需要读取原先以 `#assembly={id}` 指定的 assembly 视图
- **THEN** 该选择经结构化字段表达并被正确解析，生成的 VRN 中不含 `#`

#### Scenario: 不兼容的视图与资源组合被拒绝
- **WHEN** 调用方对某资源请求其不支持的视图
- **THEN** 系统显式失败并说明不支持，不得降级为默认视图

### Requirement: 解析必须遵循唯一星型顺序且 fail-closed

解析 MUST 按唯一顺序执行：本地 parse（fail-closed）→ 本进程解析 → 跨边界时按**星型解析 / star-topology resolution** 交给 gateway 层，由对端按同一份 VRN 在本地解析。

- **网关授权段 / gateway authority** 承载稳定 gateway_id。
- 本地 gateway **是自身联邦的 hub** 时，MUST 可直接解析其**直接 spoke** 的资源。
- 本地 gateway **是 spoke** 时，MUST 通过其**唯一 hub** 做**一次有界 transit 解析**，并 MUST 携带 `visited set`、`max_transit_gateways=1`、`max_gateway_hops=2` 与**总 deadline**。
- 上述上界 MUST 表达为**显式策略常量**，MUST NOT 硬编码为散落的魔法数字；拓扑变化时改策略而非重写解析器。
- **解析命中只返回稳定身份与内容**，MUST NOT 返回或携带 locator。这是可机械检查的不变量：**locator 是输入，不是输出**。
- 不可解析（不可达、未共享、未找到）时 MUST fail-closed 返回结构化**拒绝码 / rejection code**，MUST NOT 回退到猜测路径，MUST NOT 返回虚假默认值。

跨边界传输 MUST 只包含资源身份、VRN、revision 与内容，MUST NOT 传输 real path。

#### Scenario: 跨 gateway 只回内容
- **WHEN** VRN 的 gateway authority 指向对端且对端可达
- **THEN** 对端在本地解析该 VRN，响应中只包含稳定身份、VRN、revision 与内容，不含任何真实路径、provider locator 或解析 locator

#### Scenario: spoke 只经唯一 hub 做一次有界 transit
- **WHEN** 本地 gateway 是 spoke，请求的目标位于另一个 spoke
- **THEN** 解析经其唯一 hub 做一次有界 transit，携带 visited set、`max_transit_gateways=1`、`max_gateway_hops=2` 与总 deadline，不得继续递归到第四个 gateway

#### Scenario: hub 可直接解析直接 spoke
- **WHEN** 本地 gateway 是自身联邦的 hub，请求目标为其直接 spoke 的资源
- **THEN** 本地 gateway 直接解析，不额外经过第二个 transit

#### Scenario: 命中不返回 locator
- **WHEN** 任意跨边界解析命中
- **THEN** 响应只含稳定身份与内容，既不含 real path 也不含任何 locator 形式

#### Scenario: 不可达时 fail-closed
- **WHEN** VRN 的 gateway authority 指向的对端不可达、未共享该资源或目标资源不存在
- **THEN** 系统返回结构化拒绝码，且不猜测路径、不返回默认内容

#### Scenario: 本地 parse 先行
- **WHEN** 调用方提交非法或不完整 VRN
- **THEN** 系统在发起任何跨进程或跨 gateway 转发之前就本地显式拒绝

### Requirement: 持久化资源引用必须使用身份加 VRN

**归属与引用**：持久化资源引用政策的正名 normative 定义见 `add-unified-virtual-resource-addressing` 的 requirement「默认寻址政策必须以 VRN 为默认形式」。本 capability 只**具名引用**该 requirement，MUST NOT 复述正文、MUST NOT 另立第二套持久化引用政策；会话上下文侧的落点由以下 scenario 验收。

#### Scenario: 字段不含 real path
- **WHEN** 会话上下文资源引用被写入持久化记录
- **THEN** 该字段由资源身份、VRN 与独立 revision 组成，不含任何真实路径

### Requirement: 旧式上下文 URI 只能被入口拒绝，且已确证无历史落盘实例

系统 MUST 停止接受旧式上下文 URI 形态（含 `%` 编码、`#fragment`、未登记 scope 的自有正则语法），入口 MUST 显式拒绝并指向结构化字段表示。

**已确证的存量事实（本轮实测取证）**：旧式会话上下文 URI **没有任何持久化实例**，因此本 capability MUST NOT 要求对历史记录做数据迁移。取证：

- 旧形态字符串（自有正则 `app/services/business/session_context_resource.py:12-16`、`ParsedSessionContextResource.canonical`、`session_context_query_service.py` 的 `locator` 字段、`app/services/business/session_context_projection.py:317` 与 `session_context_query_service.py:481` 的 base64 `next_cursor`）**全部在请求/响应链路内构造并随响应返回调用方**，不存在把它写入 session/catalog/checkpoint/rollout 存储的代码路径；
- 全仓唯一承载 `boxteam://` 的持久化列是 `resource_activation_bindings.display_uri`（`resource_activation_schema.py:85`），其唯一写入方 `ResourceActivationStore.persist_snapshot` 的**调用方全在测试**（`tests/unit/services/infrastructure/rollout_context/test_resource_activation_storage.py`、`test_resource_activation_retention.py`、`test_resource_activation_fork_identity.py`三处调用 `attach_resource_activation_store` / `ResourceActivationStore(...)`），`app/container.py` 未装配（`rg resource_activation app/container.py` 退出 1），生产 seal 链路从未传入 `activation_snapshot`（恒为默认 `None`），故该列在生产中从不被写入；
- 磁盘取证：157 个 live 库 + 44 个 dev/temp 库中 **0 个 activation 表、0 个 `boxteam://` 命中**，`out/development-runtime` 的真实 `rollout.jsonl` 与 `tests/fixtures/` 亦 0 命中；
- 会话上下文分页游标从不落盘：全仓不存在承载 `SessionContextCursorCodec` 输出的持久化列（唯一 `cursor` 持久化列 `workspace_event_cursors.cursor_value` 是工作区活动 `event_seq`，与上下文游标无关）。

因此本 capability 的迁移面只有两点：(a) 入口对旧形态 fail-closed 拒绝；(b) 既有持久化字段（`display_uri` 列、`context_source_control_states` 的来源事实）改为按新格式**新写入**并在读路径切换——属「新写字段」，不是「存量数据迁移」。

#### Scenario: 旧式 fragment 形态被拒绝
- **WHEN** 调用方提交 `boxteam://session/{session_id}#assembly={id}` 一类旧式 URI
- **THEN** 系统显式拒绝并提示使用新的资源身份加结构化字段表示，不得按新语法静默解释该字符串

#### Scenario: 不构造历史数据迁移
- **WHEN** 实施本次寻址统一
- **THEN** 不扫描、不规范化、不失效任何既有记录，因为已确证不存在内嵌旧式上下文 URI 的持久化记录

#### Scenario: 新写入字段即迁移面
- **WHEN** 某个既有持久化字段（如 `display_uri`、来源事实）需要承载资源引用
- **THEN** 它以资源身份加 VRN 的新格式写入并在读路径切换，旧写入形态物理下线，不留兼容层

### Requirement: 配置来源的真实路径持久化必须迁移到 VRN

**归属与引用**：本义务的正名 normative 出处为 `add-unified-virtual-resource-addressing` 的 requirement「既有配置来源持久化必须按同一模式迁移为 VRN 兄弟字段」（含 config kind、尾段形态、`sqlite` 层不可寻址与 real path 不持久化的全部正文）。本 capability 只**具名引用**该 requirement，MUST NOT 复述其正文、MUST NOT 另立第二套 config 迁移规范；以下 scenario 只保留会话上下文侧的验收视角。

#### Scenario: 配置来源记录不含真实路径
- **WHEN** 配置来源层被持久化到 SQLite
- **THEN** 记录中用 VRN 表达来源，不含 `source_path`/`backup_path` 一类真实路径字段

#### Scenario: API 不输出配置来源真实路径
- **WHEN** 客户端请求配置来源列表（`GET /api/v1/config/sources`）
- **THEN** 响应体只含 VRN 与兄弟字段（layer/precedence/layer_revision/layer_digest/source_generation 等），不含真实路径

#### Scenario: sqlite 层不编 VRN
- **WHEN** 迁移处理 `user` / `user_local` / `workspace` 这些共享同一 `workspace.sqlite` 的来源
- **THEN** 不为该 sqlite 文件编造 VRN，并显式说明其共享载体导致的不可寻址性

#### Scenario: 复用既有 sibling 字段形态
- **WHEN** 实施把配置来源改造成 VRN 表达
- **THEN** 直接以 `ConfigSource` 既有的平级 `layer`/`precedence`/`layer_revision`/`layer_digest`/`source_generation` 兄弟字段承载，不新增第二套结构

### Requirement: 已确证义务与 owner 已定稿项必须显式区分

本 capability MUST 把**已确证**的结论写成可执行任务或 normative 断言，MUST 把**已由「统一虚拟资源寻址」change 定稿**的内容（scope 闭集、scope_id 语义、kind 闭集、拒绝码）写为具名引用；MUST NOT 把已定稿项写成本地待定。**本 capability 无待该 owner 裁定的项。** 会话上下文资源**自身**的 kind 已由「统一虚拟资源寻址」change 在 kind 闭集内定稿为 `session`（见其 requirement「kind 闭集定稿且描述符闭集独立不可混用」），本 capability 直接引用、无需新登记。

已确证（本轮实测取证）：

- 旧式会话上下文 URI **零历史落盘实例**，迁移面只有「入口拒绝 + 新写字段」；
- 配置来源真实路径**已持久化且已外泄**（`ConfigSourceLayerRecord.source_path`/`backup_path` 与 `app/api/config.py:102` 的 `ConfigSourceDTO.path`）；
- `inline` 层有稳定 disk 载体、`sqlite` 层是共享载体不可寻址，`memory` 不是 VRN scope。

由「统一虚拟资源寻址」change 定稿并引用（本 capability 只引用、不得自行发明）：scope 闭集（`workspace` | `user` | `gateway` | `inline`，见其 requirement「scope 必须取自定稿闭集且 scope_id 对所有 scope 必填」）、每个 scope 的 scope_id 语义（同前 requirement）、`kind` 闭集（见其 requirement「kind 闭集定稿且描述符闭集独立不可混用」）、以及全部拒绝码取值（见其拒绝码登记 requirement）。

会话上下文资源**自身**使用的 `kind` 取值已由「统一虚拟资源寻址」change 在 kind 闭集内**定稿为 `session`**（见其 requirement「kind 闭集定稿且描述符闭集独立不可混用」，闭集为 `agent-spec` | `skills` | `config` | `session`）。本 capability **直接引用该已登记取值，无需新登记、无待裁定项**；规范形态为 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/session/{...canonical path segments}`。

#### Scenario: owner 已定稿项写为具名引用
- **WHEN** 某内容属于 scope 闭集、scope_id 语义、`kind` 闭集或拒绝码取值
- **THEN** 它具名引用「统一虚拟资源寻址」change 对应 requirement，MUST NOT 写成本地待定或另行发明

#### Scenario: 会话上下文 kind 引用已登记取值
- **WHEN** 会话上下文资源自身的 `kind` 取值被引用
- **THEN** 它具名引用「统一虚拟资源寻址」change 已定稿的 `session`，本 change 不自行发明取值、不再列作待裁定

#### Scenario: 已确证结论可直接执行
- **WHEN** 某内容已由本轮实测取证确证
- **THEN** 它可直接写成 normative 断言或可执行任务，不再列作待验证
