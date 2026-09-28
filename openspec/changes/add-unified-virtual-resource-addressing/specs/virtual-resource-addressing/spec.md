## Purpose

为软件内部与模型可见载荷中的资源引用建立**唯一**的寻址抽象与词汇：严格区分资源身份（ResourceIdentity）、虚拟资源地址（VRN）与真实路径（real path），把 `workspace`、`user`、`gateway`、`inline` 等作用域与其它工作区、其它 gateway 收敛到同一套寻址，并以顶层 gateway 之间的**星型解析**完成跨边界定位。本 capability 是这套抽象、scope 闭合集、VRN 语法与拒绝码登记处的唯一 owner。

**契约版本**：本 capability 采用**契约修正 v2**（取代 v1 的简化段序模板）。v2 明确：`resources` 是保留固定段，`scope_id` 对**所有** scope 都必填。**scope 名、scope_id 语义与拒绝码的最终权威以 owner 随后下发的权威表为准**；本 spec 中这三类内容为**初审状态**，权威表下发前不得据此实现。`memory` 作用域**未经本次重新定义**，本 capability 不对其做设计、不定其 scope_id。

## ADDED Requirements

### Requirement: 三层职责必须严格分离

系统 MUST 把资源引用区分为三层，每层有唯一 owner 且职责不可互换：

- **资源身份 / ResourceIdentity**：不透明、稳定、**不含 revision**、**不依赖当前激活工作区**，持久化，用于去重与 lineage。
- **虚拟资源地址 / VRN**：可解析的地址，持久化，**允许悬空**（指向已不存在的资源是合法值），**禁止编码 revision 或 hash**，是软件内部与模型可见载荷中传递资源的默认形式。
- **真实路径 / real path**：机器本地、**临时**、**永不持久化**、**永不进入模型可见载荷**、**永不跨 gateway 边界**，只作为 fs/sqlite 调用点内的局部变量存在。

#### Scenario: real path 只作为局部变量存在

- **WHEN** 某个 owner 需要访问底层文件或 sqlite 资源
- **THEN** 它先由 identity + VRN 解析出 real path，且该 real path 只在该调用栈内使用，不写入任何持久化记录、不写入 API 响应体、不写入模型可见载荷

#### Scenario: real path 外泄即缺陷

- **WHEN** 一次 API 响应体、一条持久化记录或一份模型可见载荷中出现了 real path
- **THEN** 系统 MUST 将其判定为缺陷并显式失败，不得以 `display_uri`、日志脱敏或截断静默掩盖

#### Scenario: VRN 允许悬空

- **WHEN** 一条持久化的 VRN 指向的资源配置已被删除
- **THEN** 该 VRN 仍是合法值，读取时返回结构化「未找到」拒绝码，而不是把 VRN 判定为格式非法

#### Scenario: revision 不进入 VRN

- **WHEN** 系统需要表达某资源的精确 revision 或 hash
- **THEN** 它使用与 VRN 并列的独立 revision/hash 字段，绝不把 revision/hash 编码进 VRN 字符串

#### Scenario: identity 不承担寻址职责

- **WHEN** 调用方持有 ResourceIdentity 但需要访问资源
- **THEN** 它仍然通过 VRN（或 VRN 解析链）定位资源，不得把 identity 当作可解析地址；反之 VRN 也不得被当作资源身份用于去重

### Requirement: scope 必须显式且 scope_id 对所有 scope 必填

VRN 的 scope MUST 取自闭合集（初审：`workspace` | `user` | `gateway` | `inline` | `memory`，最终以权威表为准）。`scope_id` 段 **MUST 对所有 scope 都出现且必填**，其取值来源由 owner 的**唯一一张 scope_id 表**规定（初审：`workspace`→workspace_id、`gateway`→`local`、`user`→`local`、`inline`→distribution_id；`memory` 待定）。「当前工作区」不是寻址概念，MUST NOT 作为持久化数据的隐含前提。其它工作区 MUST 复用 `workspace` scope 加另一个 `workspace_id` 表达，MUST NOT 引入新 scope。

**正名与收敛**：`builtin` MUST 更名为 `inline`，向 config 域既有词汇（`inline`/`user`/`user_local`/`workspace`/`sqlite`）收敛；`user` 为新增，命名依据同一 config 域前缀。`memory` 既有存在但**未经本次重新定义**，MUST NOT 基于它做设计、MUST NOT 把它当作文件 locator、MUST NOT 为它定义 scope_id。

#### Scenario: scope_id 对所有 scope 必填

- **WHEN** 系统构造或解析任意 scope 的 VRN
- **THEN** 路径中 MUST 出现显式 `scope_id` 段；任何省略 `scope_id`、或依赖「当前激活工作区 / 当前 gateway / 当前发行版」补齐缺省 scope_id 的解析一律被拒绝

#### Scenario: workspace scope 的 scope_id 是 workspace_id

- **WHEN** 解析一个 `workspace` scope 的 VRN
- **THEN** `scope_id` 段被解释为 workspace_id；同 gateway 下的其它工作区就是另一个 `workspace_id`，不新增 scope

#### Scenario: inline 取代 builtin

- **WHEN** 系统需要表达发行包内置层的资源
- **THEN** 它使用 `inline` scope（scope_id 为 distribution_id），并 MUST NOT 继续产出或接受 `builtin` 作为 scope 名，也不保留 `builtin`→`inline` 的运行时别名

#### Scenario: 未知 scope 被拒绝

- **WHEN** 解析遇到闭合集以外的 scope 段
- **THEN** 系统返回 scope 未登记的结构化拒绝码，且不尝试任何猜测映射

#### Scenario: memory 未被本次重新定义

- **WHEN** 本次寻址改造涉及 `memory` 作用域
- **THEN** 系统 MUST NOT 基于它做设计、MUST NOT 把它作为文件 locator、MUST NOT 为它指派 scope_id；其归属等待 owner 的统一规定

### Requirement: VRN 语法必须保留固定段序并单一实现

统一 VRN 语法 MUST 为：

```text
boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}
```

其中 `{gateway_authority?}` 为**可选单段**，承载**稳定 gateway_id**，缺省即本机 gateway；`resources` 是**固定保留段**（MUST NOT 省略、MUST NOT 被简化掉）；`{kind}` 取自闭合集（初审在既有基础上新增 `config`，最终以权威表为准）。系统 MUST 使用闭合 charset；MUST 拒绝百分号编码与 `#fragment`；大小写、分隔符与相对段的规范化 MUST 只有单一实现。现有 skill 形态 `boxteam://workspace/{workspace_id}/resources/skills/{name}/SKILL.md` MUST 是本语法的特例（authority 缺省），MUST NOT 存在第二套并列语法。

#### Scenario: resources 固定段不可省略

- **WHEN** 解析一个 VRN
- **THEN** `resources` 段按固定位置参与解析；缺少该段的字符串被判定为格式非法并结构化拒绝，而不是回退到更宽松的旧形态

#### Scenario: 既有 skill 形态是特例

- **WHEN** 系统处理一个不带 gateway authority 的 skill VRN
- **THEN** 它由同一条语法规则解析，authority 取本机缺省，而非走独立的历史分支

#### Scenario: 拒绝百分号编码与 fragment

- **WHEN** VRN 字符串包含 `%` 或 `#`
- **THEN** 系统在访问任何资源前显式拒绝，分别返回百分号编码拒绝与 fragment 拒绝的结构化拒绝码

#### Scenario: 规范化只有单一实现

- **WHEN** 同一逻辑地址以不同大小写或冗余分隔符表达
- **THEN** 所有调用方得到由同一实现产出的同一规范化结果；MUST NOT 存在第二处独立的大小写或分隔符处理

#### Scenario: 新增 config kind

- **WHEN** 系统表达一条配置来源资源的地址
- **THEN** 其 kind 使用闭合集内的 `config`，与既有 kind 共享同一套语法与拒绝码

#### Scenario: 未登记 kind 被拒绝

- **WHEN** VRN 的 kind 段不在已登记闭集内
- **THEN** 系统返回 kind 未登记的结构化拒绝码，不回退到通用资源读写

### Requirement: gateway authority 承载稳定 gateway_id 且缺省等价本机

VRN 的可选 gateway authority 段 MUST 承载**稳定 gateway_id**：段缺省表示本机 gateway；段等于本机 gateway_id 与缺省等价；段等于对端 gateway_id 表示跨 gateway。系统 MUST NOT 为「其它 gateway」引入新 scope，MUST NOT 把 authority 段与 scope 段混为一谈，MUST NOT 让 authority 承载瞬时通道标识（channel instance/epoch/route）。

#### Scenario: authority 缺省等价本机

- **WHEN** VRN 不含 authority 段
- **THEN** 系统按本机 gateway 解析，且与显式写本机 gateway_id 产生相同的解析结果

#### Scenario: 对端 authority 触发跨 gateway 解析

- **WHEN** VRN 的 authority 段等于一个对端 gateway_id
- **THEN** 系统进入跨 gateway 解析链，判定依据仅为该 authority 段，不依据 scope 或 kind

#### Scenario: authority 不承载瞬时通道标识

- **WHEN** 一次跨 gateway 解析经过某条具体通道
- **THEN** 通道实例/epoch/路由 locator MUST NOT 出现在 VRN、持久化记录或业务幂等键中；authority 只表达稳定 gateway_id

### Requirement: VRN 解析必须是星型且以 policy 常量界定上界

VRN 解析 MUST 按唯一顺序执行：本地 parse（fail-closed）→ 无 authority 或 authority 等价本机时由本进程按 workspace registry 解析 → authority 指向对端时按联邦关系转发：本地 gateway 是自身联邦的 **hub** 时可直接解析其**直接 spoke** 的资源；本地 gateway 是 **spoke** 时通过其**唯一 hub** 做**一次有界 transit 解析** → 不可达、未共享或未找到时 MUST fail-closed 返回结构化拒绝码。系统 MUST NOT 回退到本地猜测路径、空路径或任何虚假默认值。

跨边界 transit MUST 携带 `visited set`、`max_transit_gateways`、`max_gateway_hops` 与**总 deadline**，且这些上界 MUST 是**显式策略常量**（集中定义），MUST NOT 硬编码为散落的魔法数字；拓扑变化时 MUST 改策略而非重写解析器。

**解析命中只返回稳定身份与内容，不返回、不携带 locator**：`locator 是输入，不是输出` 是本 capability 可机械检查的不变量。

#### Scenario: 跨 gateway 只传逻辑事实

- **WHEN** 解析请求跨过 gateway 边界
- **THEN** 边界两侧只传递 identity、VRN、revision 与资源内容，MUST NOT 传递 real path、provider locator 或 credential

#### Scenario: hub 可直接解析直接 spoke

- **WHEN** 本地 gateway 是其联邦的 hub，且 authority 指向一个直接 spoke
- **THEN** 系统按直接 spoke 关系解析，不额外经过第三方中转

#### Scenario: spoke 经唯一 hub 有界 transit

- **WHEN** 本地 gateway 是 spoke，且 authority 指向一个非直接对端
- **THEN** 系统通过其唯一 hub 做一次有界 transit 解析，并携带 `visited set`、`max_transit_gateways=1`、`max_gateway_hops=2` 与总 deadline；MUST NOT 继续递归转发到第四个 gateway

#### Scenario: locator 不是输出

- **WHEN** 一次解析（含跨 gateway 命中）成功返回
- **THEN** 其返回值只含稳定身份与内容，不含任何 locator；locator 仅作为本次解析的输入存在

#### Scenario: 超过上界显式失败

- **WHEN** 解析需要的中继次数或跳数超过 policy 常量，或总 deadline 耗尽
- **THEN** 系统 fail-closed 返回结构化拒绝码，MUST NOT 以本地同名资源、空结果或缓存猜值替代

#### Scenario: 对端不可达时显式失败

- **WHEN** authority 指向的对端 gateway 不可达
- **THEN** 系统返回不可达的结构化拒绝码，MUST NOT 用本地同名资源、空结果或缓存猜值替代

#### Scenario: 未共享时显式失败

- **WHEN** 对端 gateway 可达但未向本机共享目标资源
- **THEN** 系统返回未共享的结构化拒绝码，对「未授权存在」与「不存在」返回同一结果，不泄露 locator

#### Scenario: 未知 gateway 显式失败

- **WHEN** authority 段指向本机未登记的 gateway_id
- **THEN** 系统返回未知 gateway 的结构化拒绝码，且不尝试按名称猜测路由

### Requirement: 拒绝码必须集中登记且命名不得自造

系统 MUST 复用既有 grammar 拒绝码的命名空间与风格，并 MUST 在**唯一一处集中登记处**登记全部拒绝码（含跨 gateway 拒绝码）。**拒绝码的具体名称与最少数量以 owner 随后下发的权威表为准**；在权威表下发前，任何模块与 change MUST NOT 自行发明或定稿新的拒绝码名。其它 change MUST 只引用集中登记处，MUST NOT 自造同义拒绝码。

#### Scenario: 拒绝码集中在唯一处登记

- **WHEN** 需要新增一个拒绝码
- **THEN** 它只在集中登记处出现一次，其它模块与其它 change 通过引用使用

#### Scenario: 权威表下发前不得自造拒绝码

- **WHEN** 某个调用方在权威表下发前需要表达一种新的拒绝
- **THEN** 它 MUST 引用集中登记处、等待权威表补齐，MUST NOT 自行发明名称或定义同义码

#### Scenario: 拒绝码闭合

- **WHEN** 系统抛出一个寻址拒绝
- **THEN** 其拒绝码 MUST 属于已登记闭合集，未登记的码 MUST 被视为实现缺陷而显式失败

### Requirement: identity 必须独立于 VRN 且不跨 scope 混同

ResourceIdentity MUST 不透明、稳定且 revision-free。同一逻辑名出现在两个不同 scope（例如 `user` 与某个 `workspace`）时 MUST 是两个不同 identity。跨来源的等价与覆盖 MUST 是独立 concern，MUST NOT 塞进 identity 或 VRN 语义。

#### Scenario: 同名跨 scope 是两个 identity

- **WHEN** 同一逻辑名同时存在于 `user` scope 与某个 `workspace` scope
- **THEN** 系统为两者分配不同 ResourceIdentity，且任一方的解析不因另一方存在而改变

#### Scenario: 跨来源覆盖不改变 identity

- **WHEN** 高优先级来源覆盖低优先级来源的同名资源
- **THEN** 覆盖只影响后续 VRN 解析结果与 catalog 快照，既有 identity 与既有已封存绑定保持稳定

### Requirement: 默认寻址政策必须以 VRN 为默认形式

软件内部的配置、skill、状态与资源引用 MUST 默认以 VRN 传递；real path MUST 只在最后访问点出现。任何新增持久化字段若需定位资源，MUST 使用 `identity + VRN(+ 独立 revision 字段)` 组合，MUST NOT 存储 real path。

#### Scenario: 新增持久化字段不得存 real path

- **WHEN** 一个 change 或实现需要新增一个用于定位资源的持久化字段
- **THEN** 它存 identity 与 VRN（必要时加独立 revision 字段），不存 real path、不存 provider locator

#### Scenario: 模型可见载荷只带 VRN

- **WHEN** 资源引用进入模型可见的 prompt、工具结果或历史投影
- **THEN** 它们携带 VRN 与独立 revision 标识，MUST NOT 携带 real path 或 credential

### Requirement: 既有配置来源持久化必须按同一模式迁移为 VRN 兄弟字段

系统 MUST 消除已存在的 real path 持久化违约：配置来源层记录中把 real path 换成 VRN，并**直接复用 config 侧既有的平级属性模式**（`path` + `layer` + `precedence` 平级，`layer_revision` / `layer_digest` / `source_generation` 已是兄弟字段），即「把 `path` 换成 `vrn`，其余 sibling 字段原样保留」。MUST NOT 另发明第二套表示。

#### Scenario: 配置来源不再持久化 real path

- **WHEN** 一条配置来源被写入持久化记录
- **THEN** 记录中承载 VRN 而非 real path；real path 只在读取该来源内容时于调用栈内出现

#### Scenario: 复用既有平级属性模式

- **WHEN** 实现配置来源的 VRN 化
- **THEN** 它沿用既有 `path`→`vrn` 的平级属性替换，其余 sibling 字段（layer、precedence、revision、digest、generation）原样保留，不新增第二套结构

### Requirement: 多工作区场景下寻址层必须显式承载 scope_id 身份

在一个后端进程可挂载多个工作区的前提下，scope_id 身份 MUST 在寻址层显式表达（HTTP API 与 VRN 皆然）。系统 MUST NOT 依赖「当前激活工作区」作为持久化数据的前提。

#### Scenario: 单进程多工作区各自显式寻址

- **WHEN** 同一后端进程同时挂载多个工作区
- **THEN** 每条对工作区资源的 VRN 都在 `scope_id` 段显式携带对应 workspace_id，解析结果不随「当前激活工作区」切换而改变

#### Scenario: 持久化数据不绑定激活态

- **WHEN** 一条持久化记录引用工作区资源
- **THEN** 其含义只由记录内显式的 scope/scope_id 与 VRN 决定，与记录写入时或读取时的「当前激活工作区」无关
