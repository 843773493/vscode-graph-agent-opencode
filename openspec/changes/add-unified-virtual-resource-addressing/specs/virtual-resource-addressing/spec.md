## Purpose

为软件内部与模型可见载荷中的资源引用建立**唯一**的寻址抽象与词汇：严格区分资源身份（ResourceIdentity）、虚拟资源地址（VRN）与真实路径（real path），把 `workspace`、`user`、`gateway`、其它工作区与其它 gateway 收敛到同一套寻址，并以顶层 gateway 之间的**星型解析**完成跨边界定位。本 capability 是这套抽象、scope 闭合集、VRN 语法与拒绝码命名空间的唯一 owner。

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

### Requirement: scope 必须是三值闭合集且 workspace 显式

VRN 的 scope MUST 是闭合集 `workspace` | `user` | `gateway` 之一。`workspace` scope 的路径 MUST 显式携带 `workspace_id`；`user` scope 指向用户级根（`${BOXTEAM_HOME:-~/.boxteams}/`）；`gateway` scope 指向 Gateway 控制面。「当前工作区」不是寻址概念，MUST NOT 作为持久化数据的隐含前提。其它工作区 MUST 复用 `workspace` scope 加另一个 `workspace_id` 表达，MUST NOT 引入新 scope。

#### Scenario: workspace 路径必须显式携带 workspace_id

- **WHEN** 系统构造或解析一个 `workspace` scope 的 VRN
- **THEN** 路径中 MUST 出现显式 `workspace_id` 段；任何依赖「当前激活工作区」补齐缺省 workspace_id 的解析一律被拒绝

#### Scenario: 其它工作区复用 workspace scope

- **WHEN** 需要引用同 gateway 下的另一个工作区资源
- **THEN** 系统使用 `workspace` scope 加该另一个 `workspace_id`，不新增 scope 值

#### Scenario: 未知 scope 被拒绝

- **WHEN** 解析遇到 `workspace`/`user`/`gateway` 以外的 scope 段
- **THEN** 系统返回 scope 未登记的结构化拒绝码，且不尝试任何猜测映射

### Requirement: VRN 语法必须单一实现并规范化

统一 VRN 语法 MUST 为：

```text
boxteam://[{gateway_authority}]/{scope}/[{workspace_id}/]{kind}/{...canonical path segments}
```

系统 MUST 使用闭合 charset 与闭合 kind 集；MUST 拒绝百分号编码与 `#fragment`；大小写、分隔符与相对段的规范化 MUST 只有单一实现。现有 skill 形态 MUST 是该语法的特例（authority 缺省），MUST NOT 存在第二套并列语法。

#### Scenario: 拒绝百分号编码与 fragment

- **WHEN** VRN 字符串包含 `%` 或 `#`
- **THEN** 系统在访问任何资源前显式拒绝，分别返回百分号编码拒绝与 fragment 拒绝的结构化拒绝码

#### Scenario: 规范化只有单一实现

- **WHEN** 同一逻辑地址以不同大小写或冗余分隔符表达
- **THEN** 所有调用方得到由同一实现产出的同一规范化结果；MUST NOT 存在第二处独立的大小写或分隔符处理

#### Scenario: 既有 skill 形态是特例

- **WHEN** 系统处理一个不带 gateway authority 的 skill VRN
- **THEN** 它由同一条语法规则解析，authority 取本机缺省，而非走独立的历史分支

#### Scenario: 未登记 kind 被拒绝

- **WHEN** VRN 的 kind 段不在已登记闭集内
- **THEN** 系统返回 kind 未登记的结构化拒绝码，不回退到通用资源读写

### Requirement: gateway authority 必须表达跨边界且缺省等价本机

VRN 的可选 gateway authority 段 MUST 表达目标 gateway：段缺省表示本机；段等于 self 表示等价本机；段等于对端表示跨 gateway。系统 MUST NOT 为「其它 gateway」引入新 scope，MUST NOT 把 authority 与 scope 混为一谈。

#### Scenario: authority 缺省等价本机

- **WHEN** VRN 不含 authority 段
- **THEN** 系统按本机解析，且与显式写本机 authority（self）产生相同的解析结果

#### Scenario: 对端 authority 触发跨 gateway 解析

- **WHEN** VRN 的 authority 段指向一个对端 gateway
- **THEN** 系统进入跨 gateway 解析链，判定依据仅为该 authority 段，不依据 scope 或 kind

### Requirement: VRN 解析必须是星型且 fail-closed

VRN 解析 MUST 按唯一顺序执行：本地 parse（fail-closed）→ 无 authority 或 authority 等价本机时由本进程按 workspace registry 解析 → authority 指向对端时交给 gateway 层解析器转发，由对端本地解析并以 identity/VRN/revision/内容应答 → 不可达、未共享或未找到时 MUST fail-closed 返回结构化拒绝码。系统 MUST NOT 回退到本地猜测路径、空路径或任何虚假默认值。

#### Scenario: 跨 gateway 只传逻辑事实

- **WHEN** 解析请求跨过 gateway 边界
- **THEN** 边界两侧只传递 identity、VRN、revision 与资源内容，MUST NOT 传递 real path、provider locator 或 credential

#### Scenario: 对端不可达时显式失败

- **WHEN** authority 指向的对端 gateway 不可达
- **THEN** 系统返回不可达的结构化拒绝码，MUST NOT 用本地同名资源、空结果或缓存猜值替代

#### Scenario: 未共享时显式失败

- **WHEN** 对端 gateway 可达但未向本机共享目标资源
- **THEN** 系统返回未共享的结构化拒绝码，对「未授权存在」与「不存在」返回同一结果，不泄露 locator

#### Scenario: 未知 gateway 显式失败

- **WHEN** authority 段指向本机未登记的 gateway
- **THEN** 系统返回未知 gateway 的结构化拒绝码，且不尝试按名称猜测路由

#### Scenario: 星型拓扑不递归转发

- **WHEN** 一次跨 gateway 解析需要经过中间 gateway
- **THEN** 中间 gateway 最多中继一次，MUST NOT 继续向第三方 gateway 递归转发

### Requirement: 拒绝码必须集中登记且风格统一

系统 MUST 复用既有 grammar 拒绝码的命名空间与风格；只有当跨 gateway 解析确需时，才新增**最少数量**的拒绝码（`unknown_gateway`、`remote_unreachable`、`remote_not_shared`），且新增码 MUST 在本 capability 内集中登记于**唯一一处**。其它 change MUST 只引用这些码，MUST NOT 自造同义拒绝码。

#### Scenario: 新增拒绝码集中在唯一处登记

- **WHEN** 需要新增一个跨 gateway 拒绝码
- **THEN** 它只在寻址 capability 的集中登记处出现一次，其它模块与其它 change 通过引用使用

#### Scenario: 不复用同义码

- **WHEN** 某个调用方需要对「对端未共享」表达拒绝
- **THEN** 它使用已登记的 `remote_not_shared`，而不是自造新的相近码

#### Scenario: 拒绝码闭合

- **WHEN** 系统抛出一个寻址拒绝
- **THEN** 其拒绝码 MUST 属于已登记闭合集，未登记的码 MUST 被视为实现缺陷而显式失败

### Requirement: identity 必须独立于 VRN 且不跨 scope 混同

ResourceIdentity MUST 不透明、稳定且 revision-free。同一逻辑名出现在 `user` 与 `workspace` 两个 scope 时 MUST 是两个不同 identity。跨来源的等价与覆盖 MUST 是独立 concern，MUST NOT 塞进 identity 或 VRN 语义。

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

### Requirement: 多工作区场景下寻址层必须显式承载 workspace 身份

在一个后端进程可挂载多个工作区的前提下，workspace 身份 MUST 在寻址层显式表达（HTTP API 与 VRN 皆然）。系统 MUST NOT 依赖「当前激活工作区」作为持久化数据的前提。

#### Scenario: 单进程多工作区各自显式寻址

- **WHEN** 同一后端进程同时挂载多个工作区
- **THEN** 每条对工作区资源的 VRN 都显式携带对应 `workspace_id`，解析结果不随「当前激活工作区」切换而改变

#### Scenario: 持久化数据不绑定激活态

- **WHEN** 一条持久化记录引用工作区资源
- **THEN** 其含义只由记录内显式的 workspace 身份与 VRN 决定，与记录写入时或读取时的「当前激活工作区」无关

