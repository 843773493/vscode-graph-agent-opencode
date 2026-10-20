## Purpose

定义「一个后端进程挂载多个工作区」这一运行形态的**workspace 身份层**：进程内权威的已挂载工作区注册表、workspace 身份在 HTTP API 上的显式寻址载体、工作区根目录与 `.boxteam/` 数据目录的定位规则、按 workspace_id 分区的进程级资源边界、Gateway 显式传目标的角色变化，以及破坏性迁移与回滚边界。本 capability 是 workspace 身份在寻址层的唯一 owner，只引用「统一资源寻址」change 的 `作用域 / scope`、`拒绝码 / rejection code` 与术语（含`资源身份 / ResourceIdentity`、`资源 URI`、`真实路径 / real path`、`三层分离 / three-layer separation`、`网关授权段 / gateway authority`、`星型解析 / star-topology resolution`），不重新定义 URI 语法。

## ADDED Requirements

### Requirement: 进程内必须维护权威的已挂载工作区注册表

一个工作区后端进程 MUST 维护当前 generation 的**已挂载工作区 runtime 注册表**，每项按稳定 `workspace_id` 对应已挂载根目录。该 registry 是本 generation 内请求解析的权威 runtime view，但 MUST NOT 持久化物理根或成为根绑定来源；根 MUST 来自下述唯一原生 Gateway binding。工作区根目录与 `.boxteam/` 数据目录的定位 MUST 由「显式 workspace_id → 当前 runtime registry」得出，MUST NOT 依赖进程级单例、环境变量或「当前激活工作区」推断。

#### Scenario: 两个工作区同进程挂载

- **WHEN** 同一后端进程挂载工作区 A 与工作区 B
- **THEN** runtime registry 同时挂载 A 与 B 的 `workspace_id` 和其启动时从各自原生 Gateway binding 得到的根，任一工作区请求都解析到自己的根且互不串扰；进程退出后根绑定仍只由唯一 Gateway registry 持有

#### Scenario: 未登记工作区显式失败

- **WHEN** 一个请求携带的 workspace 身份不在注册表中
- **THEN** 后端 MUST fail-closed 返回结构化的「未登记工作区」错误，MUST NOT 回退到默认工作区、单一根目录或任何猜测值

#### Scenario: 根目录解析无进程级前提

- **WHEN** 后端需要某工作区的根目录或 `.boxteam/` 数据目录
- **THEN** 它 MUST 以显式 `workspace_id` 查当前 generation runtime registry 得到，MUST NOT 读取进程级全局工作区根；runtime 项缺失时必须报错，不扫描磁盘寻找根

### Requirement: 工作区物理根必须绑定到唯一原生 Gateway

每个本地托管或显式注册到本机的工作区物理根 MUST 只持久化在负责该原生 target 的唯一 Gateway 控制面 registry 中，并按 Gateway target ID 与 Backend `workspace_id` 绑定。该 binding 是 Gateway 重启和 Backend 冷启动的唯一根路径来源；Managed Backend group MUST 只保存成员 target ID、Backend `workspace_id` 与 generation，不复制物理根；Backend 的 generation-local runtime registry MAY 在进程生命周期内持有启动所得的根，但 MUST NOT 作为持久化 authority 或恢复来源。

用户授权的任意外部工作区注册与冷启动 MUST 保留。本机管理请求使用现有本地 managed token；该 token 代表本 Gateway 的管理权限，允许在本机注册、替换或移除 root binding。不得为此另建 RBAC、权限注册表或路径 ACL。

远端注册、替换或移除 MUST 由承载目标工作区的原生 Gateway 做最终授权决定。该 Gateway MUST 复用唯一既有 `permissions.federation` policy owner（`FederationPolicyStore`）；它 MUST 从已认证的 federation peer 身份调用既有 `principal_ref_for_gateway(authenticated_peer.gateway_id)` 派生 `principal_ref`，并要求现有策略规则显式允许 `capability="managed_workspace_admin"`。该权限按接收请求的目标 Gateway 授予，不按 workspace、路径或工作区资源 ACL 授予；首次注册时 `workspace_id` 尚不存在，授权判定 MUST 仅依赖已认证 peer principal 和该目标 Gateway 的 policy。不得新增第二个 policy owner、RBAC/权限注册表或路径 ACL。该 capability 的配置 schema、规范化/snapshot/evaluator 与全部注册、替换、移除调用方 MUST 在同一迁移批次闭合。

`permissions.federation.default_effect="allow"` 对既有 `discovery|send|read|wait|reply|transit` 通信能力的含义与默认值 MUST 保持不变，但 MUST NOT 授予 `managed_workspace_admin`；只有命中以该 peer `principal_ref` 为主体的显式 `effect="allow"` 规则才能授予它。peer 配对成功或拥有 discovery/read 等通信权限 MUST NOT 推导管理权限。发往 relay 的请求 MUST 由 relay 透明转送，relay MUST NOT 替目标 Gateway 作出授权决定或把本地规则当作目标授权。未获授权的远端操作 MUST 在写入前返回 HTTP 403，且不得修改 root binding registry、managed Backend group 合同、成员集合或 generation。

注册请求携带用户提供的物理根时，原生 Gateway owner MUST 按上述权限合同核验后才写入唯一 binding；中继 Gateway MUST NOT 持久化该根或把它加入联邦资源 projection。物理根 MUST NOT 编码进 URI，也 MUST NOT 由扫描、默认路径、同名路径或其它推测产生。读取/解析资源的权限 MUST NOT 推导创建、替换或移除 root binding 的授权。

workspace runtime API、普通 Workspace DTO、解析结果、模型载荷和跨 Gateway 资源投影 MUST NOT 暴露物理根。启动或解析时若 binding 缺失、target 对应的 Backend `workspace_id` 不匹配，系统 MUST 显式失败，MUST NOT adopt 部分 mount、回退到其它根或扫描磁盘。

#### Scenario: 本地 managed token 授权本 Gateway 管理

- **WHEN** 调用方使用本 Gateway 现有本地 managed token 请求注册、替换或移除本 Gateway 所管理 target 的 root binding
- **THEN** 该 token 代表本 Gateway 的管理权限，由本机原生 Gateway owner 执行变更并只在唯一 registry 持久化 root；不要求另配 federation rule 或第二套权限记录

#### Scenario: 远端管理操作由目标 Gateway 的显式 policy 授权

- **WHEN** 已认证的 federation peer 请求注册、替换或移除工作区 root binding，接收该请求的原生 Gateway policy 对 `principal_ref_for_gateway(authenticated_peer.gateway_id)` 显式允许 `managed_workspace_admin`
- **THEN** 目标 Gateway 按其本地 policy 作最终授权决定并执行唯一 registry 变更；若请求经过 relay，relay 只转送请求，不代替目标作授权

#### Scenario: 默认通信允许或 peer 配对不授予 root 管理权限

- **WHEN** 已认证 peer 已配对，且 `default_effect="allow"` 或显式规则允许其 discovery/read，但没有匹配该 peer 的 `managed_workspace_admin` 显式 allow 规则；该 peer 尝试注册、替换或移除 root binding
- **THEN** 原生目标 Gateway MUST 在写入前返回 HTTP 403；root binding registry、managed Backend group 合同、成员集合与 generation 均保持不变。有效认证、配对、资源读取权限及通用默认 allow MUST NOT 被视为管理授权

#### Scenario: 冷启动只使用登记根绑定

- **WHEN** Gateway 冷启动或恢复一个 managed Backend generation
- **THEN** 它 MUST 按完整成员身份从唯一 registry 取得每个 root binding 并启动完整 mount 集；任何 binding 缺失或 Backend UUID 不匹配都 MUST 显式失败，MUST NOT 扫描磁盘、猜测或部分启动

#### Scenario: 文件资源解析不返回物理根

- **WHEN** 用户经 URI 解析或读取 workspace `files/root`
- **THEN** 原生 owner 在本地用绑定根执行访问，但 resolver、API、模型载荷和跨 Gateway projection 只包含 URI/身份/内容/操作结果，不包含物理 root

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

### Requirement: workspace_id 必须与 URI workspace scope 使用同一份身份

`workspace_id` MUST 是**寻址层身份**。HTTP API 中显式携带的 `workspace_id` 与**资源 URI** 的 `workspace` **作用域 / scope** 里**必填**的 `scope_id` MUST 是同一个稳定 `workspace_id`（`workspace` scope 的 `scope_id` 必填且等于 workspace_id，是 requirement「`scope_id` 必须推导自该 scope 的稳定身份，禁止硬编码」的一个实例；语法本体与取值来源表属「统一资源寻址」change）；两处 MUST NOT 各自为政或存在换算层。跨 gateway 引用另一工作区时，MUST 使用同一台机器上那个稳定 `workspace_id`，否则跨 gateway 寻址从根上不成立。本 capability MUST NOT 定义 URI 语法、`作用域 / scope` 闭合集、`scope_id` 语义或`拒绝码 / rejection code`，只引用其 owner change。

#### Scenario: 同一工作区在两处取值一致

- **WHEN** 一个工作区同时被 HTTP 请求显式寻址与 URI 显式寻址
- **THEN** 两处使用的 `workspace_id` MUST 相等（URI 侧即 `workspace` scope 的必填 `scope_id`），且 MUST 能被同一注册表解析到同一根

#### Scenario: 不定义第二套身份

- **WHEN** 需要表达「另一个工作区」
- **THEN** 系统 MUST 复用同一 `workspace_id` 身份与 URI `workspace` scope 的 `scope_id`，MUST NOT 引入第二套工作区标识、别名或映射表

### Requirement: scope_id 必须推导自该 scope 的稳定身份，禁止硬编码

**资源 URI** 的 `scope_id` 段对所有 scope MUST 必填，且 MUST **推导自该 scope 的稳定身份**；MUST NOT 使用硬编码字面量、进程级单例或「当前激活」态补齐。`workspace` **作用域 / scope** 的 `scope_id` MUST 等于显式 workspace_id（本 capability 的核心），此原则对其它 scope **同构**：`gateway` scope 的 `scope_id` MUST 推导自真实 gateway_id，`inline` scope 的 `scope_id` MUST 推导自真实 distribution_id（其来源与编码 MUST 按「统一资源寻址」change 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」从发行包 runtime manifest 推导，本 capability 只引用、不复述取值规则）。本 capability 只声明该推导原则与两个既存违反点的归属；`作用域 / scope` 闭合集终值、各 scope 的 `scope_id` 取值表与 `拒绝码 / rejection code` 登记，以「统一资源寻址」change 的权威表为准。

#### Scenario: scope_id 推导自稳定身份而非硬编码

- **WHEN** 系统为任一 scope 构造**资源 URI**
- **THEN** `scope_id` MUST 由该 scope 的稳定身份推导得到，MUST NOT 是硬编码字面量、进程级单例或「当前激活」态

#### Scenario: workspace scope 是同一推导原则的实例

- **WHEN** `workspace` scope 的 `scope_id` 被解析
- **THEN** 它 MUST 等于显式 workspace_id，并与 HTTP 显式寻址载体同源（同一稳定身份）

#### Scenario: gateway 与 inline 不恢复旧硬编码

- **WHEN** 审视当前 `gateway` 与 `inline`（原 `builtin`）两个 scope 的 `scope_id` 生成
- **THEN** 二者 MUST 使用寻址 owner 已装配的真实稳定身份（gateway_id / distribution_id）；原规划基线共用 `"local"` 的违反点已修复，MUST NOT 恢复该硬编码、把历史描述当现行合同或重新建立一套身份推导

### Requirement: gateway 身份与 workspace 身份同属寻址层身份，且 gateway_id 必须真实

**网关授权段 / gateway authority** 承载稳定 gateway_id；gateway 身份与 workspace 身份 MUST 同属**寻址层身份**、遵循同一显式化原则：两者 MUST 都能由请求或持久化记录显式表达，MUST NOT 依赖进程级单例或「当前激活」态。作为本 capability 的**接口前提**：跨 gateway 的**星型解析 / star-topology resolution** 要求 gateway_id 真实可用；`distribution_id` 与 `gateway` scope 的 `scope_id` **均已装配**（`distribution_id` 由 `298ef599`+`f3bd8213` 落地、`app/core/distribution_identity.py::load_distribution_id` 为生产赋值方并被 `app/agents/skill_runtime.py::_layer_scope_identity` 消费；`gateway` scope_id 的请求级注入由 `64ba30c8`/`53befbfc`/`9881a3b2` 落地，**2026-10-01 第四轮复核更正**）。`distribution_id` 的**来源为**发行包 runtime manifest 的 `distribution` + `version`（编码规则见「统一资源寻址」change 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」），**已装配**；**`gateway_id` 的来源与注入 owner 亦已裁定**：owner = Gateway 侧按请求注入，取值按「统一资源寻址」change 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」推导（本 capability 只引用、不复述取值规则），**通道与装配均已落地**。两者均已就绪，可支撑跨 gateway 寻址。本 capability 只登记该前提与影响面，不实现其取值来源。

#### Scenario: gateway 身份与 workspace 身份同属寻址层身份

- **WHEN** 系统表达一次跨 gateway 的目标工作区引用
- **THEN** gateway 身份 MUST 与 workspace 身份一样显式可表达，MUST NOT 由任何进程级「当前 gateway」状态补齐

#### Scenario: gateway_id 通道与装配已落地

- **WHEN** 跨 gateway 寻址依赖 `gateway` scope 的 `scope_id`
- **THEN** 该值 MUST 是真实 gateway_id，取值与注入按「统一资源寻址」change 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」建立（owner = Gateway 侧按请求注入），**当前装配已落地**（提交 `64ba30c8`/`53befbfc`/`9881a3b2`，原硬编码字面量分支已物理删除），MUST NOT 被当作靠硬编码字面量已成立

#### Scenario: distribution_id 装配已落地

- **WHEN** 系统需要为 `inline` scope 推导 `scope_id`
- **THEN** 其来源 MUST 取发行包 runtime manifest 的 `distribution` + `version`（编码规则见「统一资源寻址」change 的 requirement），**当前装配已落地**（提交 `298ef599`+`f3bd8213`），MUST NOT 以硬编码字面量冒充已推导

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

Gateway MUST 继续负责选择目标工作区，但选定目标 MUST 显式传递给后端（经本 capability 规定的显式载体）。后端 MUST NOT 猜测、推断或回退目标工作区。Gateway 的生命周期所有权 MUST NOT 再以「一个工作区一个后端进程」为隐含前提。跨 gateway 时**网关授权段 / gateway authority**承载**稳定 gateway_id**，拓扑是**星型解析 / star-topology resolution**（hub-spoke）；解析命中 MUST 只返回稳定的**资源身份 / ResourceIdentity** 与内容，MUST NOT 返回或携带 locator（locator 是输入不是输出）；不可解析 MUST fail-closed。解析链本体属「统一资源寻址」change，本 capability 只要求 Gateway 的目标传递与其一致。

#### Scenario: Gateway 显式传目标

- **WHEN** Gateway 将请求代理给后端
- **THEN** 请求 MUST 显式携带目标 workspace 身份，后端按该身份解析根

#### Scenario: 后端不猜目标

- **WHEN** 后端收到一个未显式携带目标 workspace 身份的代理请求
- **THEN** 后端 MUST 显式拒绝，MUST NOT 以默认或上一次的目标补齐

#### Scenario: 进程数与工作区数解耦

- **WHEN** 同一后端进程挂载多个工作区
- **THEN** Gateway MUST 能对其中任一工作区发请求，且 MUST NOT 要求为每个工作区各拉起一个后端进程；进程句柄、完整组排空/force、generation切换及恢复 MUST 遵守唯一 `managed-backend-lifecycle` capability，本 capability 不另立生命周期合同

#### Scenario: 跨 gateway 解析不返回 locator（星型解析 / star-topology resolution）

- **WHEN** Gateway 经**星型解析 / star-topology resolution** 的 hub-spoke 有界 transit 解析另一个 gateway 上的工作区资源
- **THEN** 命中结果 MUST 只包含稳定的**资源身份 / ResourceIdentity** 与内容，MUST NOT 返回或携带 locator（含本机**真实路径 / real path**）

#### Scenario: 跨 gateway 不可解析时 fail-closed

- **WHEN** 目标 gateway 未登记、不可达或未共享目标资源
- **THEN** 系统 MUST fail-closed 返回「统一资源寻址」change 登记的结构化**拒绝码 / rejection code**，MUST NOT 用本机同名资源、空结果或缓存猜值替代

### Requirement: 持久化数据不得以「当前激活工作区」为前提

新增或迁移的业务持久化字段 MUST NOT 以「当前激活工作区」作为隐含前提；凡引用工作区资源者，MUST 遵守**三层分离 / three-layer separation**：记录只承载 `资源身份 / ResourceIdentity` 与 `资源 URI`（及独立 revision 字段），其含义 MUST 只由记录内显式的 workspace 身份决定，与写入时或读取时的进程激活态无关。工作区物理根的持久化例外仅为本 capability requirement「工作区物理根必须绑定到唯一原生 Gateway」的原生 Gateway control-plane registry；它不得出现在普通 API/Workspace DTO、解析结果、模型载荷或联邦资源投影。

#### Scenario: 记录自带工作区身份

- **WHEN** 一条持久化记录引用某工作区资源
- **THEN** 其含义 MUST 由记录内显式 workspace 身份决定，切换或改变进程激活态 MUST NOT 改变其解析结果（**三层分离 / three-layer separation** 不变量）

#### Scenario: 真实路径 / real path 不落盘

- **WHEN** 系统写入一条引用工作区资源的业务持久化记录或返回普通 Workspace/API DTO
- **THEN** 其中 MUST NOT 出现**真实路径 / real path**，只允许 `资源身份 / ResourceIdentity` 与`资源 URI`（必要时加独立 revision 字段）；工作区根绑定按本 capability 的控制面 requirement 单独保存

### Requirement: 破坏性迁移与回滚必须有明确边界

本形态迁移 MUST 提供破坏性切换步骤；按用户授权，旧开发运行数据 MUST 显式失效并在停止对应写入后清理，MUST NOT 引入旧 URI 扫描、回填、兼容层、双读或别名。当前新写入和精确恢复 MUST 继续遵守显式 workspace 身份合同，不能通过清理 fresh 数据掩盖 writer 错误。原零 URI、未装配 activation 取证只属于历史基线，MUST NOT 用来否定当前已接通的 production seal/restore。只描述单工作区前提的旧持久化字段 MUST 不再读取。迁移 MUST 声明代码、数据与生成快照各自的回滚边界及不可回滚点；旧客户端调用（依赖「当前激活工作区」或旧工作区维度地址者）MUST 被视为破坏范围内。

#### Scenario: 旧开发数据不建立迁移链

- **WHEN** 本形态切换遇到不符合新合同的旧开发运行数据
- **THEN** 系统 MUST 停止对应写入并显式失效、清理这些数据，只保留新写字段与单一读路径，MUST NOT 扫描回填、迁移旧 URI 或保留双读；普通源码和独有未集成改动 MUST 保留

#### Scenario: 旧字段显式失效

- **WHEN** 迁移遇到只描述单工作区前提的旧持久化字段
- **THEN** 系统 MUST 将其显式失效并删除旧读取分支，MUST NOT 静默按旧语义继续解释

#### Scenario: 不提供兼容层

- **WHEN** 迁移完成后收到依赖旧单工作区形态的调用
- **THEN** 系统 MUST 显式失败，MUST NOT 提供旧形态兼容分支、双读或别名

#### Scenario: 回滚边界已声明

- **WHEN** 迁移执行到任一步骤
- **THEN** 必须能明确说明该步是否可回滚及回滚后的状态，不可回滚点 MUST 显式标注

未注册目录不是本 capability 的 mount 或工作区注册，不得伪造 workspace_id 或增加 WorkspaceTarget。其 URI、唯一原生 Gateway 控制面目录根绑定及路径持久化例外只引用 `add-unified-virtual-resource-addressing` requirement「原生目录根绑定必须由目标 Gateway 唯一持有」，不改变本 capability 的 workspace-root binding 合同。
