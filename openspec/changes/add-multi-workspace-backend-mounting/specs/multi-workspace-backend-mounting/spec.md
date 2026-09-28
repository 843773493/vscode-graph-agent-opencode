## Purpose

定义「一个后端进程挂载多个工作区」这一运行形态的**workspace 身份层**：进程内权威的已挂载工作区注册表、workspace 身份在 HTTP API 上的显式寻址载体、工作区根目录与 `.boxteam/` 数据目录的定位规则、按 workspace_id 分区的进程级资源边界、Gateway 显式传目标的角色变化，以及破坏性迁移与回滚边界。本 capability 是 workspace 身份在寻址层的唯一 owner，只引用「统一虚拟资源寻址」change 的 `作用域 / scope`、`拒绝码 / rejection code` 与术语（含`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`三层分离 / three-layer separation`、`网关授权段 / gateway authority`、`星型解析 / star-topology resolution`），不重新定义 VRN 语法。

## ADDED Requirements

### Requirement: 进程内必须维护权威的已挂载工作区注册表

一个工作区后端进程 MUST 维护一张进程内权威的**已挂载工作区注册表**，每项至少包含稳定的 `workspace_id` 与其根目录。工作区根目录与 `.boxteam/` 数据目录的定位 MUST 由「显式 workspace_id → 注册表」得出，MUST NOT 依赖进程级单例、环境变量或「当前激活工作区」推断。

#### Scenario: 两个工作区同进程挂载

- **WHEN** 同一后端进程挂载工作区 A 与工作区 B
- **THEN** 注册表同时持有 A 与 B 的 `workspace_id` 与各自根目录，任一工作区的请求都解析到自己的根，互不串扰

#### Scenario: 未登记工作区显式失败

- **WHEN** 一个请求携带的 workspace 身份不在注册表中
- **THEN** 后端 MUST fail-closed 返回结构化的「未登记工作区」错误，MUST NOT 回退到默认工作区、单一根目录或任何猜测值

#### Scenario: 根目录解析无进程级前提

- **WHEN** 后端需要某工作区的根目录或 `.boxteam/` 数据目录
- **THEN** 它 MUST 以显式 `workspace_id` 查注册表得到，MUST NOT 读取进程级全局工作区根

### Requirement: workspace 身份必须由显式寻址载体承载

按工作区维度操作的 HTTP API MUST 显式携带 workspace 身份。规范载体 MUST 是路径段 `/api/v1/workspaces/{workspace_id}/...`；Gateway 内部代理层 MAY 使用 `X-BoxTeam-Workspace-Id` 请求头作为等价载体。两个载体 MUST 指向同一注册表项，MUST NOT 各自为政或产生不同的解析结果。

#### Scenario: 路径段与头一致

- **WHEN** 同一请求同时带有路径段 workspace 身份与等效请求头 workspace 身份
- **THEN** 两者 MUST 指向同一注册表项；不一致时 MUST 显式失败，MUST NOT 静默取其一

#### Scenario: 缺失 workspace 身份被拒绝

- **WHEN** 一个按工作区维度操作的请求未携带任何显式 workspace 身份
- **THEN** 后端 MUST 显式拒绝，MUST NOT 用「当前激活工作区」或默认工作区补齐

#### Scenario: 不再使用激活工作区

- **WHEN** 后端处理一个工作区维度请求
- **THEN** 其语义 MUST 只由请求中显式的 workspace 身份决定，与进程内任何「激活」状态无关

### Requirement: workspace_id 必须与 VRN workspace scope 使用同一份身份

`workspace_id` MUST 是**寻址层身份**。HTTP API 中显式携带的 `workspace_id` 与**虚拟资源地址 / VRN** 的 `workspace` **作用域 / scope** 里**必填**的 `scope_id` MUST 是同一个稳定 `workspace_id`（`workspace` scope 的 `scope_id` 必填且等于 workspace_id，是 requirement「`scope_id` 必须推导自该 scope 的稳定身份，禁止硬编码」的一个实例；语法本体与取值来源表属「统一虚拟资源寻址」change）；两处 MUST NOT 各自为政或存在换算层。跨 gateway 引用另一工作区时，MUST 使用同一台机器上那个稳定 `workspace_id`，否则跨 gateway 寻址从根上不成立。本 capability MUST NOT 定义 VRN 语法、`作用域 / scope` 闭合集、`scope_id` 语义或`拒绝码 / rejection code`，只引用其 owner change。

#### Scenario: 同一工作区在两处取值一致

- **WHEN** 一个工作区同时被 HTTP 请求显式寻址与 VRN 显式寻址
- **THEN** 两处使用的 `workspace_id` MUST 相等（VRN 侧即 `workspace` scope 的必填 `scope_id`），且 MUST 能被同一注册表解析到同一根

#### Scenario: 不定义第二套身份

- **WHEN** 需要表达「另一个工作区」
- **THEN** 系统 MUST 复用同一 `workspace_id` 身份与 VRN `workspace` scope 的 `scope_id`，MUST NOT 引入第二套工作区标识、别名或映射表

### Requirement: scope_id 必须推导自该 scope 的稳定身份，禁止硬编码

**虚拟资源地址 / VRN** 的 `scope_id` 段对所有 scope MUST 必填，且 MUST **推导自该 scope 的稳定身份**；MUST NOT 使用硬编码字面量、进程级单例或「当前激活」态补齐。`workspace` **作用域 / scope** 的 `scope_id` MUST 等于显式 workspace_id（本 capability 的核心），此原则对其它 scope **同构**：`gateway` scope 的 `scope_id` MUST 推导自真实 gateway_id，`inline` scope 的 `scope_id` MUST 推导自真实 distribution_id（其来源与编码 MUST 按「统一虚拟资源寻址」change 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」从发行包 runtime manifest 推导，本 capability 只引用、不复述取值规则）。本 capability 只声明该推导原则与两个既存违反点的归属；`作用域 / scope` 闭合集终值、各 scope 的 `scope_id` 取值表与 `拒绝码 / rejection code` 登记，以「统一虚拟资源寻址」change 的权威表为准。

#### Scenario: scope_id 推导自稳定身份而非硬编码

- **WHEN** 系统为任一 scope 构造**虚拟资源地址 / VRN**
- **THEN** `scope_id` MUST 由该 scope 的稳定身份推导得到，MUST NOT 是硬编码字面量、进程级单例或「当前激活」态

#### Scenario: workspace scope 是同一推导原则的实例

- **WHEN** `workspace` scope 的 `scope_id` 被解析
- **THEN** 它 MUST 等于显式 workspace_id，并与 HTTP 显式寻址载体同源（同一稳定身份）

#### Scenario: gateway 与 inline 的既存硬编码必须被登记为违反点

- **WHEN** 审视当前 `gateway` 与 `inline`（原 `builtin`）两个 scope 的 `scope_id` 生成
- **THEN** 实测二者共用同一硬编码字面量（`"local"`）而各自稳定身份（gateway_id / distribution_id）未被推导；本 capability MUST 将其登记为违反「`scope_id` 必须推导自稳定身份」的既有点，MUST NOT 把该硬编码形态当作契约

### Requirement: gateway 身份与 workspace 身份同属寻址层身份，且 gateway_id 必须真实

**网关授权段 / gateway authority** 承载稳定 gateway_id；gateway 身份与 workspace 身份 MUST 同属**寻址层身份**、遵循同一显式化原则：两者 MUST 都能由请求或持久化记录显式表达，MUST NOT 依赖进程级单例或「当前激活」态。作为本 capability 的**接口前提**：跨 gateway 的**星型解析 / star-topology resolution** 要求 gateway_id 真实可用；而权威表实测 `gateway` scope 的 `scope_id` 为硬编码字面量（与 `inline`/原 `builtin` 逐字相同），且 `distribution_id` 全仓**无生产赋值方**（仅在字段定义、resolver 读取与测试中出现）。`distribution_id` 的**来源已裁定**为发行包 runtime manifest 的 `distribution` + `version`（编码规则见「统一虚拟资源寻址」change 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」），剩装配；**`gateway_id` 的来源与注入 owner 亦已裁定**：owner = Gateway 侧按请求注入，取值按「统一虚拟资源寻址」change 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」推导（本 capability 只引用、不复述取值规则），剩通道与装配。两者就绪后可支撑跨 gateway 寻址。本 capability 只登记该前提与影响面，不实现其取值来源。

#### Scenario: gateway 身份与 workspace 身份同属寻址层身份

- **WHEN** 系统表达一次跨 gateway 的目标工作区引用
- **THEN** gateway 身份 MUST 与 workspace 身份一样显式可表达，MUST NOT 由任何进程级「当前 gateway」状态补齐

#### Scenario: gateway_id 来源已裁定，剩通道与装配

- **WHEN** 跨 gateway 寻址依赖 `gateway` scope 的 `scope_id`
- **THEN** 该值 MUST 是真实 gateway_id，取值与注入必须按「统一虚拟资源寻址」change 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」建立（owner = Gateway 侧按请求注入）；在此之前当前硬编码字面量的形态 MUST 被视为**未满足的接口前提**，MUST NOT 被当作跨 gateway 寻址已成立

#### Scenario: distribution_id 来源已裁定，剩装配

- **WHEN** 系统需要为 `inline` scope 推导 `scope_id`
- **THEN** 其来源 MUST 取发行包 runtime manifest 的 `distribution` + `version`（编码规则见「统一虚拟资源寻址」change 的 requirement），在此之前 MUST NOT 以硬编码字面量冒充已推导

### Requirement: 进程级资源必须按 workspace_id 分区

多工作区同进程挂载时，进程级缓存、单例与注册表 MUST 按 `workspace_id` 分区；同一份进程级状态 MUST NOT 被两个工作区共享为可变事实。至少 MUST 覆盖：会话目录解析器与其缓存、SQLite 状态库与进程所有权锁、会话生命周期 gate 与 operation lease、Job 事件总线与事件通道、后台任务注册表、配置服务与其工作区根绑定、工作区活动/资源注册表、持久资源账本、消息流与 trace 存储。

#### Scenario: 解析器按工作区分区

- **WHEN** 进程为工作区 A 与工作区 B 各自解析会话目录
- **THEN** 两工作区 MUST 使用各自独立的解析器实例与其 catalog 连接，MUST NOT 复用同一实例或同一缓存键

#### Scenario: 进程所有权锁按工作区分区

- **WHEN** 进程持有某工作区状态库的进程所有权锁
- **THEN** 该锁 MUST 只覆盖该工作区自己的状态库，MUST NOT 阻塞或以另一工作区的状态冒充本工作区

#### Scenario: 事件总线与后台任务分区

- **WHEN** 工作区 A 与工作区 B 各自产生 Job 事件或后台任务
- **THEN** 事件与任务 MUST 归属各自工作区的总线与注册表，MUST NOT 串入另一工作区的流

### Requirement: Gateway 必须显式传目标工作区且后端不得猜

Gateway MUST 继续负责选择目标工作区，但选定目标 MUST 显式传递给后端（经本 capability 规定的显式载体）。后端 MUST NOT 猜测、推断或回退目标工作区。Gateway 的生命周期所有权 MUST NOT 再以「一个工作区一个后端进程」为隐含前提。跨 gateway 时**网关授权段 / gateway authority**承载**稳定 gateway_id**，拓扑是**星型解析 / star-topology resolution**（hub-spoke）；解析命中 MUST 只返回稳定的**资源身份 / ResourceIdentity** 与内容，MUST NOT 返回或携带 locator（locator 是输入不是输出）；不可解析 MUST fail-closed。解析链本体属「统一虚拟资源寻址」change，本 capability 只要求 Gateway 的目标传递与其一致。

#### Scenario: Gateway 显式传目标

- **WHEN** Gateway 将请求代理给后端
- **THEN** 请求 MUST 显式携带目标 workspace 身份，后端按该身份解析根

#### Scenario: 后端不猜目标

- **WHEN** 后端收到一个未显式携带目标 workspace 身份的代理请求
- **THEN** 后端 MUST 显式拒绝，MUST NOT 以默认或上一次的目标补齐

#### Scenario: 进程数与工作区数解耦

- **WHEN** 同一后端进程挂载多个工作区
- **THEN** Gateway MUST 能对其中任一工作区发请求，且 MUST NOT 要求为每个工作区各拉起一个后端进程

#### Scenario: 跨 gateway 解析不返回 locator（星型解析 / star-topology resolution）

- **WHEN** Gateway 经**星型解析 / star-topology resolution** 的 hub-spoke 有界 transit 解析另一个 gateway 上的工作区资源
- **THEN** 命中结果 MUST 只包含稳定的**资源身份 / ResourceIdentity** 与内容，MUST NOT 返回或携带 locator（含本机**真实路径 / real path**）

#### Scenario: 跨 gateway 不可解析时 fail-closed

- **WHEN** 目标 gateway 未登记、不可达或未共享目标资源
- **THEN** 系统 MUST fail-closed 返回「统一虚拟资源寻址」change 登记的结构化**拒绝码 / rejection code**，MUST NOT 用本机同名资源、空结果或缓存猜值替代

### Requirement: 持久化数据不得以「当前激活工作区」为前提

新增或迁移的持久化字段 MUST NOT 以「当前激活工作区」作为隐含前提；凡引用工作区资源者，MUST 遵守**三层分离 / three-layer separation**：记录只承载 `资源身份 / ResourceIdentity` 与 `虚拟资源地址 / VRN`（及独立 revision 字段），其含义 MUST 只由记录内显式的 workspace 身份决定，与写入时或读取时的进程激活态无关。真实路径 / real path MUST NOT 出现在持久化记录或 API 响应体中。

#### Scenario: 记录自带工作区身份

- **WHEN** 一条持久化记录引用某工作区资源
- **THEN** 其含义 MUST 由记录内显式 workspace 身份决定，切换或改变进程激活态 MUST NOT 改变其解析结果（**三层分离 / three-layer separation** 不变量）

#### Scenario: 真实路径 / real path 不落盘

- **WHEN** 系统写入一条引用工作区资源的持久化记录或返回一个 API 响应体
- **THEN** 其中 MUST NOT 出现**真实路径 / real path**，只允许 `资源身份 / ResourceIdentity` 与`虚拟资源地址 / VRN`（必要时加独立 revision 字段）

### Requirement: 破坏性迁移与回滚必须有明确边界

本形态迁移 MUST 提供破坏性迁移步骤，但 MUST NOT 虚构不存在的存量数据：权威表实测**虚拟资源地址 / VRN 零落盘**（live 安装工作区 157 个 SQLite 与 dev/temp 44 个库对 `boxteam://` 零命中、无 `resource_activation*` 表；唯一 `display_uri` 列的写入方全在测试、container 无装配、生产 seal 恒为 `None`），故本 change 的 workspace 身份显式化**不涉及存量 VRN 数据迁移**，只需新写字段与读路径切换。既有**只描述单工作区前提**的非 VRN 持久化字段仍 MUST 显式迁移或显式失效，MUST NOT 保留旧形态兼容层、双读或别名。迁移 MUST 声明可回滚到何种程度（含不可回滚点与其理由）。旧客户端调用（依赖「当前激活工作区」或旧工作区维度地址者）MUST 被视为破坏范围内。

#### Scenario: 无存量 VRN 数据需要迁移

- **WHEN** 本形态迁移启动并核对磁盘
- **THEN** 依据实测的 VRN 零落盘事实，迁移 MUST 不引入任何 VRN 存量读取/改写步骤，只做新写字段与读路径切换；若实施时实测发现存量，MUST 显式迁移或显式失效，MUST NOT 双读

#### Scenario: 旧字段显式失效

- **WHEN** 迁移遇到只描述单工作区前提的旧持久化字段
- **THEN** 系统 MUST 显式迁移或将其标记失效，MUST NOT 静默按旧语义继续解释

#### Scenario: 不提供兼容层

- **WHEN** 迁移完成后收到依赖旧单工作区形态的调用
- **THEN** 系统 MUST 显式失败，MUST NOT 提供旧形态兼容分支、双读或别名

#### Scenario: 回滚边界已声明

- **WHEN** 迁移执行到任一步骤
- **THEN** 必须能明确说明该步是否可回滚及回滚后的状态，不可回滚点 MUST 显式标注
