## Purpose

定义会话上下文资源的统一寻址与解析合同：资源位置一律以虚拟资源地址（VRN）表达，修订、视图与游标作为并列的结构化字段，从而在同一套资源语法上同时满足「地址不编码 revision」与「可重读的修订绑定 locator」两个约束。

## ADDED Requirements

### Requirement: 资源引用必须遵循三层分离

系统 MUST 将每个资源引用拆分为三层唯一 owner，任一层 MUST NOT 承担另一层的职责：

- **资源身份 / ResourceIdentity**：不透明、稳定、**不含 revision**、**不依赖当前激活工作区**；MUST 持久化；用于去重与 lineage。
- **虚拟资源地址 / VRN**：可解析地址；MUST 持久化且**允许悬空**（登记地址后资源可暂不可达）；**MUST NOT 编码 revision、hash、快照引用或 provider locator**；是软件内部与模型可见载荷传递资源的**默认**形式。
- **真实路径 / real path**：机器本地、**临时**；MUST NOT 被持久化，MUST NOT 进入模型可见载荷，MUST NOT 跨越 gateway 边界。

real path 出现在 API 响应体、持久化记录或模型可见载荷中 MUST 视为缺陷。

#### Scenario: 持久化记录不得含 real path
- **WHEN** 任意会话上下文资源被写入持久化记录或返回给模型
- **THEN** 记录中只出现资源身份与 VRN（以及独立的 revision 字段），不出现任何真实路径、绝对路径或 provider locator

#### Scenario: 资源身份不随激活工作区变化
- **WHEN** 同一资源在切换激活工作区前后被引用
- **THEN** 其资源身份保持逐字节相同，且不因当前激活工作区改变而改变

### Requirement: scope 必须取自闭合集且每个 scope 的 scope_id 一律必填

系统 MUST 只承认闭合集内的 scope，MUST 拒绝任何其它 scope 取值。scope 闭集与每个 scope 的 scope_id 取值来源 MUST 由「统一虚拟资源寻址」change 的**唯一权威表**规定；本 capability MUST NOT 自行发明 scope 名或 scope_id 语义。

**每个 scope 的 `scope_id` 段一律必填**，MUST NOT 只对某个 scope 必填而对其它 scope 可选。这条把「workspace_id 必须显式」的原则扩展到所有 scope：任何 scope 的寻址都不得依赖隐含上下文。

「当前工作区」MUST NOT 作为寻址概念的隐含前提，也 MUST NOT 作为持久化数据的隐含前提。

「其它工作区」MUST 表达为同一 scope 加另一个 scope_id 取值，MUST NOT 引入新 scope；「其它 gateway」MUST 表达为可选的**网关授权段 / gateway authority**，其缺省值为本机。

本 capability MUST NOT 依赖未经本次重新定义的 scope（例如既有 `memory`）作设计，MUST NOT 把其当作文件 locator，也 MUST NOT 为它规定 scope_id。

#### Scenario: 拒绝未登记的 scope
- **WHEN** 调用方提交 scope 不属于闭合集的 VRN
- **THEN** 解析在访问任何 provider 之前以结构化拒绝码显式失败，不读取文件、网络或内存资源

#### Scenario: 任何 scope 缺少 scope_id 都必须失败
- **WHEN** 调用方提交的 VRN 在 scope 段之后缺少 scope_id 段（对任意 scope，而非仅某一种）
- **THEN** 解析显式失败，且不得回退到隐含上下文补全

### Requirement: VRN 语法形态统一且由单一 owner 规范化

会话上下文资源 MUST 使用统一 VRN 形态（保留既有段序，MUST NOT 简化）：

`boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}`

- `{gateway_authority?}`：可选、**单段**，承载稳定 gateway_id；缺省 = 本机 gateway。
- `{scope}`：必填，取自闭合集。
- `{scope_id}`：必填，**对所有 scope 都必填**。
- `resources`：固定段，MUST 保留。
- `{kind}`：必填，取自闭合集。
- `{...canonical path segments}`：canonical 尾段。

VRN 的字符集 MUST 为闭合集合；解析 MUST 拒绝 `%` 百分号编码与 `#fragment`；规范化 MUST 由**单一实现**完成。

本 capability MUST NOT 定义 VRN 语法本体，MUST NOT 自行变更固定段序，MUST NOT 把 `scope_id` 改为可选，也 MUST NOT 新增拒绝码；VRN grammar、固定段序与**拒绝码 / rejection code** 登记由「统一虚拟资源寻址」change 独占，本 capability 只引用。

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

### Requirement: 新增持久化字段必须使用身份加 VRN

新增的持久化资源引用字段 MUST 使用资源身份加 VRN 的组合表达，并 MUST 在需要修订绑定时另设**独立的 revision 字段**；MUST NOT 持久化 real path。

既有的待迁移记录若内嵌旧式上下文 URI 字符串，MUST 依据破坏性迁移规则规范化或标记失效（见本 capability 的迁移 requirement）。

#### Scenario: 新字段不含 real path
- **WHEN** 引入新的持久化资源引用字段
- **THEN** 该字段由资源身份、VRN 与独立 revision 组成，不含任何真实路径

### Requirement: 旧式上下文 URI 必须显式破坏性迁移

系统 MUST 停止接受旧式上下文 URI 形态（含 `%` 编码、`#fragment`、未登记 scope 的自有正则语法）。

对可能已持久化的旧式上下文 URI 字符串，系统 MUST 提供显式的一次性迁移：可规范化的记录 MUST 迁到新的资源身份加 VRN 表示并保留来源 lineage；**不可规范化的记录 MUST 显式标记失效并报错**，MUST NOT 被静默按新语法解释。

迁移 MUST 有明确回滚边界：迁移前的原始记录 MUST 保留至迁移整体确认成功，回滚后系统 MUST 恢复到只读旧记录的等价状态。

#### Scenario: 旧式 fragment 形态被拒绝
- **WHEN** 调用方提交 `boxteam://session/{session_id}#assembly={id}` 一类旧式 URI
- **THEN** 系统显式拒绝并提示使用新的资源身份加结构化字段表示，不得按新语法静默解释该字符串

#### Scenario: 不可规范化的历史记录显式失效
- **WHEN** 迁移遇到无法规范化的旧式上下文 URI 记录
- **THEN** 该记录被显式标记失效并报错，不得被猜测或静默丢弃

#### Scenario: 迁移可回滚
- **WHEN** 迁移在整体确认成功之前被判定失败并回滚
- **THEN** 原始旧记录仍完整可用，系统行为与迁移前等价

### Requirement: 配置来源的真实路径持久化必须迁移到 VRN

已确证存在一处真实违约：配置来源的真实路径已写入 SQLite（`app/services/infrastructure/config/state.py` 的 `ConfigSourceLayerRecord` 含 `source_path: str` 与 `backup_path: str | None`），且该真实路径还会经 API 响应体对外（`ConfigSourceDTO.path`）。按三层分离，真实路径 MUST NOT 被持久化，也 MUST NOT 进入 API 响应体。

迁移 MUST **直接复用 config 侧已跑通的形态**：`app/core/config_sources.py` 的 `ConfigSource` 已是 `path` + `layer` + `precedence` 平级属性，且 `layer_revision` / `layer_digest` / `source_generation` 已是**兄弟字段**。因此改造等价于「把 `path: Path` 换成 `vrn: VRN`，其余兄弟字段原样保留」，MUST NOT 另发明一套结构。

迁移后：来源层身份 MUST 由 VRN 表达，API 响应体 MUST NOT 输出真实路径；真实路径只允许存在于最后访问点。

#### Scenario: 配置来源记录不含真实路径
- **WHEN** 配置来源层被持久化到 SQLite
- **THEN** 记录中用 VRN 表达来源，不含 `source_path`/`backup_path` 一类真实路径字段

#### Scenario: API 不输出配置来源真实路径
- **WHEN** 客户端请求配置来源列表
- **THEN** 响应体只含 VRN 与兄弟字段（layer/precedence/layer_revision/layer_digest/source_generation 等），不含真实路径

#### Scenario: 复用既有 sibling 字段形态
- **WHEN** 实施把配置来源改造成 VRN 表达
- **THEN** 直接以 `ConfigSource` 既有的平级 `layer`/`precedence`/`layer_revision`/`layer_digest`/`source_generation` 兄弟字段承载，不新增第二套结构

### Requirement: 迁移义务与未验证项必须显式区分

本 capability MUST 把**已确证**的迁移义务（配置来源真实路径持久化）写为可执行任务，并将**尚未验证**的假设（VRN 是否已落进持久化的 session/catalog/checkpoint 数据、`ConfigSource.path` 的间接泄漏路径、`inline`/`sqlite` 两层是否有可解析载体、`memory` 的真实形态与归属）标记为待验证，MUST NOT 在 spec 中断言其结论。

#### Scenario: 未验证项不被断言
- **WHEN** 存在尚未验证的归属或形态假设
- **THEN** 该假设以待验证项出现，不得写为 normative 断言或据此设计
