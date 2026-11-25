## Purpose

定义 Gateway 与 Workspace 配置的运行时所有权、独立 schema 与版本、显式父链及配置读取边界。
## Requirements
### Requirement: 配置按运行时所有权隔离

Gateway 与 Workspace SHALL 各自从明确入口解析本域配置节点及其合法祖先。默认 Gateway 入口为 `${BOXTEAM_HOME}/config/gateway_local.jsonc`；默认 Workspace 入口为当前显式工作区的 `.boxteam/workspace.jsonc`，各默认父链由安装／初始化写明。用户可显式改入口或同域父链，不因固定文件名自动叠加其他来源。Gateway MUST NOT 读取任何工作区 `.boxteam/` 配置或代理解析 Workspace 业务配置；Backend 不读取 Gateway 配置作为默认父。

#### Scenario: 同一 Gateway 管理多个工作区
- **WHEN** Gateway 注册同一台电脑上的多个 Workspace Backend
- **THEN** Gateway 只依据自己的配置图和控制面状态路由，不读取任一工作区的 `.boxteam/workspace.jsonc`

#### Scenario: Workspace 应用本地覆盖
- **WHEN** 工作区入口明确继承用户级 Workspace 链并写入本地 settings
- **THEN** Backend 按实际单父链计算结果，当前工作区覆盖生效，其他工作区不随之改变

#### Scenario: 父链缺失而旧默认文件存在
- **WHEN** 指定入口的父文件不存在，但目录中有原固定层级文件
- **THEN** 启动明确失败，不按旧文件层级猜测替代父

### Requirement: 配置域使用独立 schema 和版本

Gateway 与 Workspace SHALL 各自使用独立 schema 和 config_version。根文档 SHALL 使用统一节点信封；settings 允许共享 network 命名空间和本域业务命名空间，Workspace 还允许 model_options 作为模型共同祖先选项，schema MUST 拒绝本地定义另一域的字段。内嵌节点版本由所属文档管理，不另建每 profile 或整棵树的版本账本。最终 schema 校验 MUST NOT 额外注入不可见默认链。

#### Scenario: Workspace 配置包含 Gateway 字段
- **WHEN** Workspace 来源在 settings 中写入 gateway 或其根文档写入不允许的跨域字段
- **THEN** 配置验证失败并报告来源 URI 与原文字段位置

#### Scenario: Gateway 配置包含 Agent 字段
- **WHEN** Gateway 来源尝试写入 workspace.agents、llm 或其他 Workspace 业务字段
- **THEN** Gateway 配置验证失败，不借通用继承放开业务所有权

#### Scenario: 节点携带另一版本的父文档
- **WHEN** 配置图引用的同域文档版本不是当前可读版本
- **THEN** 明确拒绝并指向适用的原维护入口，不合并不同版本的字段或在普通读取中重写文件

### Requirement: 旧合并配置执行可恢复布局迁移
Launcher SHALL 在服务启动前把旧用户级 `boxteam.jsonc` 拆分为新 Gateway 与 Workspace 配置，并把旧工作区级 `boxteam.jsonc` 迁移为 `workspace.jsonc`。迁移 MUST 验证全部候选，MUST 可在进程中断后幂等恢复，且 MUST NOT 静默覆盖冲突的新文件。

#### Scenario: 迁移旧用户配置
- **WHEN** 旧用户级 `boxteam.jsonc` 存在且两个新目标不存在
- **THEN** 系统先完成旧版本内容迁移，再生成并验证 `gateway.jsonc` 与 `workspace.jsonc`，确认两个目标后移除旧源

#### Scenario: 提交一个目标后中断
- **WHEN** 布局迁移在一个新目标提交后进程中断
- **THEN** 下一次启动依据迁移状态和内容摘要继续完成迁移，不覆盖不匹配的文件

#### Scenario: 新旧配置发生冲突
- **WHEN** 旧源与用户独立创建的新目标同时存在且内容不能证明一致
- **THEN** 启动失败并报告全部冲突路径，不猜测优先级

#### Scenario: 旧工作区配置越权声明 Gateway
- **WHEN** 工作区级旧 `boxteam.jsonc` 包含 `gateway`
- **THEN** 布局迁移失败并说明 Gateway 配置只能位于用户级 Gateway 文件

### Requirement: 迁移后不读取旧布局
布局迁移完成后，运行时 MUST NOT 把 `boxteam.jsonc`、`boxteam.json` 或源码模板作为配置 fallback。

#### Scenario: 迁移完成后旧文件重新出现
- **WHEN** 新配置已经存在且旧配置文件再次出现
- **THEN** 启动报告布局冲突，不把旧文件合并进有效配置

### Requirement: Workspace 配置按生效范围分类

Workspace SHALL（必须）为有效配置路径声明可观察的生效范围。模型、Agent 和开发运行参数的变化 MUST（必须）至少对后续 Job 生效；默认 Agent 的变化 MUST（必须）只影响后续新会话；UI 和按请求读取的配置变化 MUST（必须）对后续读取生效；当前 Job、当前会话的既有绑定和已经建立的请求 MUST（必须）保持原语义。产品诊断日志 SHALL 属于 Gateway 的 `logging.runtime`，不再由 Workspace logger 决定，日志等级与格式不属于 Workspace 必须重启的配置。已完成安全替换验收的 MCP Server 连接字段 SHALL（必须）按工作区受控热应用，通过目录激活边界影响后续模型调用，不重建正在运行的 Agent 或改投已封存调用；未覆盖的依赖 MUST（必须）继续明确分类为需重启，不得将未知字段一律当作可热应用。

#### Scenario: 修改模型或 Agent 配置

- **WHEN** 有效 llm 或 agents 配置热重载成功
- **THEN** 后续 Job 使用新配置，正在运行的 Job 不被替换或重建

#### Scenario: 修改默认 Agent

- **WHEN** 有效 default_agent 发生变化
- **THEN** 新建会话使用新的默认 Agent，已有会话继续使用其已持久化的 Agent 标识

#### Scenario: 修改已被启动时缓存的运行时依赖

- **WHEN** 修改按当前有效策略仍不能安全替换的 Workspace 运行时依赖
- **THEN** 系统报告 Workspace backend restart required，并不得声称该配置已经即时生效

#### Scenario: 修改产品日志等级

- **WHEN** 用户修改 Gateway 有效 `logging.runtime.level`
- **THEN** 系统走产品进程日志热应用，不要求重启 Workspace，也不改变工作区业务配置的合并顺序

#### Scenario: 修改受支持的 MCP 连接配置

- **WHEN** 有效 MCP 服务绑定、已登记连接参数、认证引用、enabled 或增删变更通过相应监督替换验收和候选准备
- **THEN** 在当前工作区受控切换、确认 active 后对后续允许边界生效，其他工作区和旧封存调用保持各自身份

#### Scenario: Server 根路径不能放行未登记字段

- **WHEN** 分类尚未登记的 MCP Server 子字段或监督字段，或者候选包含当前 schema 不接受的字段
- **THEN** Server 增删规则不递归放行未知子字段，未登记路径保持需重启分类；schema 无效候选明确报告 invalid_config，上一份 active 快照、连接和已提交事件保持原样，不将先前应用路径说成本次字段已应用

### Requirement: Gateway 配置支持安全的分级重载

Gateway SHALL（必须）监听其有效配置来源并发布配置变化结果。可独立更新的 UI、目录/生成器调度或健康检查参数 MUST（必须）在不重启整个 Gateway 的情况下应用；工作区注册、隧道和服务生命周期变化 MUST（必须）通过拥有权、租约和排空规则安全对账；无法安全替换的 Gateway 运行时依赖 MUST（必须）报告 Gateway restart required。

#### Scenario: 修改 Gateway 可热更新参数
- **WHEN** 用户修改有效的 Gateway UI 或目录调度配置
- **THEN** 后续请求或调度周期使用新值，并保留 Gateway 进程及已有会话连接

#### Scenario: 修改托管工作区配置
- **WHEN** 用户新增、更新或移除 Gateway 配置中的托管工作区
- **THEN** Gateway 按配置来源执行原子对账，新增或更新目标后再处理移除，不误删手动或系统目标

#### Scenario: 修改无法热替换的 Gateway 配置
- **WHEN** 配置变化会破坏当前代理连接、联邦凭据或无法安全重建的运行时依赖
- **THEN** Gateway 保留当前 active revision，报告 Gateway restart required 和待应用 revision

### Requirement: Gateway registry 资源必须带有所有权和租约

Gateway registry 中的每个目标 SHALL（必须）具有不可变身份、唯一 owner 类型和当前 runtime lease。工作区资源的身份 MUST 是原生 `gateway_id` 与工作区自身 UUID `workspace_id`；连接资源使用 `connection_id`，不得把连接 ID 当成工作区 ID。owner 类型至少包括 `config`、`manual`、`system` 和 `remote_projection`；配置对账只允许创建、更新和移除 `config` 目标，MUST NOT（不得）删除其他 owner 的目标。关闭 SSH 隧道或服务前 MUST（必须）确认没有活动 lease、代理请求或 SSE/WebSocket 使用者，并完成排空。

#### Scenario: 配置删除与手动目标并存
- **WHEN** 新的 Gateway 配置删除一个曾由配置创建的目标，同时 registry 中存在手动目标
- **THEN** 对账只移除对应 `config` 目标，保留 `manual`、`system` 和 `remote_projection` 目标

#### Scenario: 目标仍有活动租约
- **WHEN** 配置更新要求替换一个仍被代理请求或事件流使用的目标
- **THEN** 系统先保留旧 generation 并等待排空；排空失败时保留旧目标和 active revision，不提前关闭隧道

#### Scenario: 远程投影返回删除
- **WHEN** 远程 Gateway 的投影事件声明某个远程工作区已删除
- **THEN** 本地只能按来源 Gateway、工作区 UUID、远程投影 owner 和事件 revision 移除该投影，不得把它当作本地 `config` 目标处理

### Requirement: 配置来源必须与非配置控制面状态分离

系统 SHALL（必须）为每一种修改入口声明来源层、权威存储、expected revision/digest、是否产生配置事件和生效范围。用户级/工作区级 JSONC 与配置 API/UI 修改配置层时进入对应配置域 SQLite 和配置事件流水线；会话级 Agent 设置、Gateway 用户 UI 视图状态、registry 手动 CRUD 和远程投影 MUST（必须）保留在各自控制面，并不得伪装成 Gateway/Workspace 配置提交。

#### Scenario: Workspace runtime override API
- **WHEN** API/UI 修改一个明确属于 Workspace runtime override 的配置字段
- **THEN** 请求携带 active/layer expected revision，更新 Workspace 配置 SQLite 层并产生配置事件，按该路径策略决定后续 Job/会话或重启生效

#### Scenario: 会话级 Agent 设置
- **WHEN** 用户只修改某个既有会话的 Agent/model 设置
- **THEN** 系统使用会话存储和 session revision 更新该会话，不修改 Workspace `default_agent`，也不产生 Workspace 配置事件

#### Scenario: Gateway 用户 UI 设置
- **WHEN** 用户修改只影响个人界面布局或视图的 Gateway 设置
- **THEN** 系统使用 Gateway 用户视图状态存储和 view revision 更新，不改变 Gateway runtime 配置，也不产生 Gateway 配置事件

#### Scenario: registry 手动 CRUD
- **WHEN** 用户通过 registry CRUD 手动创建、更新或删除目标
- **THEN** 系统使用 registry revision 和 owner=`manual` 执行控制面变更；Gateway 配置 watcher 不得把它当成 config owner 或覆盖该目标

### Requirement: Workspace 配置来源层必须分别物化并支持用户级 fan-out

Workspace SHALL 分别维护实际依赖文件和已有 runtime override 的 payload、layer_revision、layer_digest、规范化来源身份、generation、presence 与 CAS 基线。user、user_local、workspace 不得物化成一个来源；自定义父文档按同规则登记。有效优先级只由真实父链决定，不再由来源类别名称决定。source 更新不等于 active 更新。

共享来源写入或 watcher 导入 SHALL 沿原流程生成 fanout_id；显式 writer 分配关联键，人工编辑由 watcher 按规范化来源路径与写后 digest 派生。该键只用于 fan-out 关联／幂等，不充当 commit revision、event cursor 或内容 digest。实际影响范围 SHALL 由实际依赖该文件的 Workspace 决定，不由 Gateway 管理列表限定；不再依赖该文件的工作区不受本次 fan-out 影响。

存活 Workspace SHALL 自行观察 active／候选图的明确文件依赖并沿原 reload 链独立校验、CAS 和应用。Gateway 重启或工作区登记在另一 Gateway 不改变来源归属，不要求 Gateway 逐个驱动。每个 Workspace 分别分配 candidate_id、commit_revision 和配置事件；未运行的 Workspace 下次启动读取最新父链和来源。不建立跨 Workspace SQLite 全局事务、成员登记或广播机制。

一个 Workspace 冲突 MUST NOT 回滚其他 Workspace 的成功导入。跨 Workspace 诊断仍可汇总 fanout_partial，但它不是单 Workspace 配置事件 result；仅汇总已观察消费者，不承诺完整未知影响范围。被后代覆盖的来源仍更新自身 revision／digest；有效值未变沿原 unchanged 合同处理，来源链改变仍须可见。

API/UI SHALL 明确选择单 Workspace 的原层物化／runtime override，或显式用户／工作区／合法父来源文件。runtime override 为图中可见的 scope 子节点，只写目标 SQLite，不回写父文件；来源文件编辑由原 writer 执行文件 CAS、原子保存和目标物化。其他 Workspace 自行观察；不得隐式改写其他层、父文件或多个 SQLite。文件已保存不代表 active 已生效。

#### Scenario: 用户级 Workspace JSONC fan-out
- **WHEN** 用户修改一次被多个 Workspace 实际继承的 workspace.jsonc，其中有运行和停止的工作区
- **THEN** 运行工作区以同一 fanout_id 独立导入，停止工作区启动时追赶；已自定义父链且不依赖此文件的工作区不受影响，各 commit_revision 互不冒充

#### Scenario: fan-out 中单个 Workspace 冲突
- **WHEN** 一个 Workspace 的 source 已被 API/UI 以更高 revision 修改，共享文件以旧基线到达
- **THEN** 该工作区报告 conflict 并保留 active/pending，其他基线有效的工作区继续，汇总可报告 fanout_partial

#### Scenario: API/UI 指定配置范围
- **WHEN** 用户选择原单工作区 runtime override 或显式来源文件保存
- **THEN** 前者只写目标 SQLite 且继承文件节点，后者沿原文件 CAS writer 保存并导入；不能把一次操作变为全图写回或跨工作区事务

#### Scenario: 明确编辑单工作区来源文件
- **WHEN** 用户绑定精确 Gateway／workspace 和来源文件，用原文件、来源和 active 基线保存局部覆盖
- **THEN** 原 writer 验证实际父链的完整候选并原子保存所选文件，另一工作区不被修改，注释、其他本地定义和秘密引用保持

### Requirement: Gateway workspaces 配置元素必须使用稳定身份

Gateway `workspaces` 配置数组的每个元素 SHALL（必须）包含持久的不可变 `connection_id`，并以该字段作为配置元素及 registry `config` 连接目标的身份。连接发现或注册的工作区 MUST 另以原生 `gateway_id` 和自身 UUID `workspace_id` 定位；不得把 `connection_id` 直接作为工作区身份，也不得派生第二个路由工作区 ID。新 Gateway 配置版本必须将 `connection_id` 纳入 schema；实现 MUST NOT（不得）使用数组下标、host、username、key 或可变连接参数推导身份。缺少该字段的受支持旧配置版本迁移 MUST（必须）在一次显式、可恢复的版本迁移中为每个无歧义元素生成并持久化连接 ID；重复、无法匹配或迁移写入失败必须拒绝，不得每次启动重新生成。

保留同一 `connection_id` 的 host、username 或 key 变化 SHALL（必须）视为同一连接的 generation 更新：新连接健康前保留旧 generation，健康后等待活动 lease、代理请求和 SSE/WebSocket 排空，再转移并关闭旧 generation。工作区身份 MUST 从已确认的原生 Gateway 和 UUID 取得，不能仅因连接 ID 不变而把不同来源工作区当作原资源。只有删除整个配置元素才允许删除对应 `config` 连接目标；`manual`、`system` 和 `remote_projection` 目标不受该对账删除。连接关系移除不得误删经其他有效连接引用的同一工作区。

#### Scenario: 连接参数变化
- **WHEN** 一个 `workspaces` 元素保持 `connection_id` 不变但修改 host、username 或 key
- **THEN** registry 原地更新同一连接的 generation，先建立并确认新 tunnel，再排空旧 tunnel；来源 Gateway 与 UUID 未变的工作区及其持久引用保持不变

#### Scenario: 配置元素删除
- **WHEN** 一个带 `connection_id` 的 `workspaces` 元素从有效配置中删除
- **THEN** 只移除对应 owner=`config` 的连接目标，且在 lease/活动请求未排空前保留旧 generation；其他 owner 的同名或相似目标不受影响，工作区投影继续由其 owner 按有效连接关系处理

#### Scenario: 旧配置迁移与重复身份
- **WHEN** 旧版本数组元素没有 `connection_id`，或迁移后两个元素使用同一 ID
- **THEN** 首次迁移持久化一次性生成的连接 ID；重复 ID 或不确定映射使迁移失败并保留旧配置，不使用数组下标兜底，不改变工作区既有 UUID

### Requirement: Gateway runtime consumer 必须分别确认应用和回滚边界

Gateway 对配置候选 SHALL（必须）按消费者分别执行 prepare、apply、健康确认、promotion 和 rollback，不得以进程内配置对象更新代替资源已生效证明：

| 消费者 | prepare/apply | promotion 条件 | 活动使用者与失败处理 |
| --- | --- | --- | --- |
| 目录/Generator scheduler | 构造新 scheduler generation，在调度边界切换 | 新周期回报 generation 和参数摘要 | 旧 scheduler 保留到边界；失败恢复旧 scheduler，无法恢复为 `recovery_required` |
| health controller | 校验 timeout/poll，创建新 controller generation | 至少一轮新参数探测成功 | 旧探测器继续服务；失败回滚旧参数 |
| registry batch | 生成完整 config-owner 目标 batch，校验 `connection_id`/owner/lease | 所有 config 目标达到可服务状态 | manual/system/remote_projection 不参与删除；批量失败反向对账，否则 `recovery_required` |
| SSH tunnel/proxy | 建立新认证连接和 runtime lease | 新 tunnel 握手成功且 lease 可转移 | 活动 HTTP/SSE/WebSocket/代理请求先排空；失败保留旧 tunnel |
| Workspace backend process | 传递 candidate_ref 启动新 generation | 返回匹配 candidate/revision/digest 的 proof | 旧进程保留至 proof；失败恢复旧进程，否则 `recovery_required` |
| remote projection | 校验 remote owner、连续 cursor 或完整快照 | 投影 batch 完成并提交新 cursor | 断档或离线保留旧投影和 lease，不做破坏性删除 |

#### Scenario: registry batch 部分资源失败
- **WHEN** registry batch 中 tunnel 或目标健康确认失败
- **THEN** 不 promotion 整个候选，已准备资源按记录的 rollback 顺序补偿，旧资源和活动 lease 保持；无法恢复时逐资源报告 `recovery_required`

#### Scenario: Gateway 热应用最终 CAS 失败
- **WHEN** scheduler、health controller 或其他 Gateway consumer 已应用候选，但 active promotion 的 source、active 或 registry CAS 失败
- **THEN** 系统使用同一 apply journal 中的 rollback 句柄恢复旧消费者；补偿成功时 journal 为 `compensated` 且候选不得成为 active，补偿失败时保留 `recovery_required` 和逐资源错误

#### Scenario: Gateway consumer fencing 失效
- **WHEN** consumer 阶段执行前，apply claim 已过期、被新 attempt 替换或 fencing token 不匹配
- **THEN** 旧阶段不得继续执行 prepare/apply/health/promotion/rollback 的可变操作；若已有副作用无法在当前 claim 下安全补偿，必须进入 `recovery_required`

#### Scenario: Gateway pending promotion 原子切换 generation
- **WHEN** 新 Gateway generation 已返回匹配 proof，但仍处于 `healthy/reserved`，旧 generation 处于 `active/serving`
- **THEN** 系统在同一 promotion 事务中提交 active snapshot、restart intent、旧 generation `draining`、新 generation `active/serving` 和 applied 事件；事务失败时旧 generation 继续 serving

### Requirement: Active promotion 前必须重新校验全部 source layer 基线

候选开始 apply 时保存的基线只用于准备，不能作为最终 promotion 的充分条件。promotion 前系统 MUST 在原配置域事务中重新校验完整候选依赖及涉及的旧依赖记录，包括 presence、revision、digest、source generation、父边与环境策略代次，以及 active／pending 和适用的 Gateway registry revision。依赖集合不再是固定来源枚举；同一 writer 的并发修改仍受原锁与 CAS 约束。

任一基线变化 MUST 阻止旧候选 promotion；不得只校验改动字段、单一叶子或新图而漏掉已移除父。外部文件读取和 watcher 导入沿原稳定读取／发布边界，不宣称任意外部文件编辑与 SQLite 是跨资源原子事务。最后检查后到达的外部改动 SHALL 按后继 source generation 导入，不反写已封存调用。

#### Scenario: 外部副作用期间 source 发生变化
- **WHEN** 候选 A apply 期间，B 改变任一父来源或将父引用指向另一个文件
- **THEN** A 最终 CAS 失败，不递增 active revision 或发布 applied paths；保留 B desired state，沿原冲突／重建处理

#### Scenario: 最终 CAS 失败前尚未产生副作用
- **WHEN** promotion 发现依赖、active 或 registry 基线不匹配，外部资源尚未切换
- **THEN** 只终止旧 apply claim 并报告 conflict/rebuild_required，不执行破坏性补偿，不改变 active

#### Scenario: 最终 CAS 失败但已有外部副作用
- **WHEN** 父依赖变化时已有 tunnel、process 或消费者完成部分切换
- **THEN** 按原 apply journal 补偿，失败进入 recovery_required，不报告已应用或静默覆盖新候选

### Requirement: Registry reconcile、manual CRUD 和 active promotion 必须使用同一 revision CAS

Gateway registry SHALL（必须）维护单调递增的 registry revision 和可恢复的 apply journal。config batch、manual CRUD、system 更新、remote projection 与 active promotion MUST（必须）声明 expected registry revision，并在同一 SQLite 事务中以 revision CAS 获取写入权；不得通过整表删除重插入绕过 revision 或覆盖其他 owner。registry MUST 按资源职责区分键：工作区资源使用 `(gateway_id, workspace_id)`，连接资源的 `connection_id` 只在明确的 config owner 命名空间中唯一。owner 和 namespace 用于管理授权与对账，不得把同一个工作区变成多个资源身份；对同一工作区的冲突登记必须明确拒绝，不得为不同 owner 各生成别名。

`mutation owner` 与目标的不可变 owner 必须分开校验。system recovery 可以更新已存在目标的运行时状态，但必须保持目标 owner、稳定 identity 和 namespace 不变；system 不得借此创建、删除或改写其他 owner 的配置目标。该更新必须继续遵守 registry revision CAS 并写入 system apply journal。

system default 工作区的生命周期字段仍归 system 所有；为保留用户对默认 Workspace 的导航体验，manual CRUD 只允许修改该工作区的名称和父级组织，且必须保持 owner、工作区 UUID、Gateway 归属、root、backend、managed 和 removable 不变。

#### Scenario: manual CRUD 与 config batch 并发
- **WHEN** config batch 已基于 registry revision N claim，manual CRUD 先提交 revision N+1
- **THEN** config batch 的最终 CAS 失败并报告 conflict，不能删除或覆盖 manual 目标；已产生的外部副作用按 journal 补偿

#### Scenario: config batch 先提交
- **WHEN** manual CRUD 使用旧 registry revision 提交，而 config batch 已提交新的 owner 目标
- **THEN** manual CRUD 被拒绝并返回当前 revision 和冲突目标，不能用旧整表快照覆盖 config batch

#### Scenario: system recovery 更新人工托管目标运行时状态
- **WHEN** Gateway 启动恢复以 mutation owner=system 更新一个已有的 owner=manual 托管工作区的 backend 地址或 connection_error
- **THEN** 更新保持工作区 owner、UUID、Gateway 归属和 namespace 不变，并以 registry revision CAS/journal 提交；system 不能借此删除、创建或改写人工目标的所有权

#### Scenario: owner 名称相同但命名空间不同
- **WHEN** manual、config 和 remote projection 使用相同显示名称或不同来源 Gateway 下相同 UUID
- **THEN** registry 依据稳定资源身份及各自 owner/namespace 执行管理对账，只删除对应 config 资源；名称相同不能导致覆盖，不同 owner 也不能为同一工作区制造不同 UUID

### Requirement: 产品诊断日志配置归 Gateway 控制面

Gateway 配置 SHALL 提供 `logging.runtime.level` 与 `logging.runtime.pretty`，默认 `info` 与 `true`，只接受有效等级和严格布尔值；配置 SHALL 覆盖本机受管产品进程。Workspace schema MUST 拒绝 logger 或 Gateway logging 字段。两个配置域 MUST 分别升级配置版本，并通过显式可恢复迁移迁出旧 logger，不得在线双读或由 Gateway 读取工作区文件。

迁移 MUST 覆盖显式选定的文件、SQLite 来源层及 active snapshot，Gateway 使用 v3、Workspace 使用 v2。预览 MUST 只读，不创建缺失数据库；来源与 active 的差异和未结算的候选／claim／恢复状态 MUST 显式报告。迁移 MUST 保持既有非日志业务配置的 active／pending 边界，历史事件与已完成 journal 不得改写。

#### Scenario: 旧配置值一致
- **WHEN** 显式迁移输入中的旧 logger 值一致且目标 Gateway 日志配置缺失
- **THEN** 迁移保留该值到 Gateway，移除旧位置并校验两域配置，重复迁移不改变结果

#### Scenario: 新旧值或不同工作区值冲突
- **WHEN** 显式迁移输入的日志设置不一致
- **THEN** 迁移列出来源和差异，要求明确统一值，不静默挑选工作区或覆盖现有目标

#### Scenario: 未迁移的旧工作区后来挂载
- **WHEN** 工作区仍包含旧 logger
- **THEN** 系统明确报告需执行迁移，不忽略该字段，也不把它用于覆盖进程日志

#### Scenario: 已有 active 快照仍包含旧日志配置
- **WHEN** 用户对已有 SQLite 配置状态执行显式日志迁移
- **THEN** 迁移同步处理来源与 active 快照及其校验基线，重启能恢复迁移后的原业务配置，不能仅改文件或顺便激活其他待生效配置

#### Scenario: 迁移预览遇到未结算应用
- **WHEN** 选定来源存在 pending_restart、应用 claim 或 recovery_required
- **THEN** 预览列出实际状态且不修改它们；迁移写入必须等原 owner 结算，不能以迁移为由删除或覆盖

### Requirement: 来源正文编辑必须由配置 owner 提供

Gateway 与 Workspace owner SHALL 公布明确目标、source key 和节点位置对应的读写能力，返回脱敏正文、schema、revision／digest、实际父链、有效值及遮蔽关系。未提供 writer 的来源保持只读；既有有限 runtime override PATCH 不扩展为任意文件／路径写入，通用文件保存不替代配置提交。来源 URI 只由原 owner 登记，不由消费者编造。

可写来源 SHALL 提供无副作用预检及文件 CAS writer。预检必须按被修改节点及其实际祖先解析完整候选，包括父引用和被引用 profile 的基线，不要求本地 settings 片段独立满足完整业务 schema。删除覆盖只删除选中节点的定义；编辑／保存保留 JSONC 注释和格式。脱敏占位不得作为原值保存，秘密仍由实际 owner 处理。

#### Scenario: 旧编辑基线与真实文件发生冲突
- **WHEN** 同一来源的另一编辑器或外部文件操作已修改正文，旧草稿提交
- **THEN** 原 writer 拒绝旧文件／source／active 基线，保留草稿并允许重读原目标，不自动重基或覆盖

#### Scenario: 脱敏正文保存与删除覆盖
- **WHEN** 来源含秘密引用和注释，用户只修改普通字段或删除本地覆盖
- **THEN** 原秘密与未改注释保持，删除后显示真实父链结果，不保存遮罩为密码，不修改其他来源

#### Scenario: 只读来源与无副作用预检
- **WHEN** 预检合法草稿，或尝试保存无 writer 的来源
- **THEN** 预检不改变文件、SQLite、active 或运行资源；只读保存明确拒绝，不使用通用文件 API 绕过

#### Scenario: Gateway 来源编辑缺少新连接身份
- **WHEN** 草稿连接未填写 connection_id 且原身份 owner 无法明确匹配
- **THEN** 返回原409合同及位置，文件、SQLite 和运行状态保持，不按数组位置借身份或在预检随机创建身份

#### Scenario: 来源保存需要先完成旧版本迁移
- **WHEN** 提交版本需旧配置转换或连接身份补齐，普通 writer 无法按当前 schema 解释
- **THEN** 拒绝并指向适用的原迁移入口，不在保存后悄悄重写成另一份正文，历史维护入口保持其支持合同

#### Scenario: 修改父引用失败
- **WHEN** 用户将父指向缺失、越权或有循环的节点
- **THEN** 预检拒绝并定位来源链，原 JSONC 草稿可修正，文件和 active 均未改变

### Requirement: 受管服务配置必须分离目标登记与工作区业务输入

Gateway SHALL 持有自有插件来源、安装／服务登记、实例范围、诊断设置及生命周期配置，Workspace SHALL 继续持有 MCP 参数、工作区使用关系、私有输入及业务配置。两域各按本域实际父链解析，并保持独立 CAS 和 active／pending；Gateway MUST NOT 读取工作区 `.boxteam/`、复制秘密或直接改写工作区来源。

插件启用关系 SHALL 复用原配置 owner，记录 package_id／plugin_id、确切包版本、固定 Gateway／workspace 范围和 enabled；无包级／单项贡献第二 enabled，纯声明插件不伪造运行进程。manifest schema 仅描述合法字段，用户值仍属上述原来源域；Workbench 中的贡献注册是可释放展示投影，不形成第二份可生效配置或跨窗口业务账本。

参数 SHALL 在原配置域正式登记的 package_id／plugin_id 参数容器中保存，与版本绑定分离；未绑定或未安装插件的残留保留原来源，明确未生效，不进入 effective／active／pending／热应用或占包引用。原 source writer 仍验证容器结构／JSON 类型；重新显式绑定按确切版本 schema 完整校验，失败不启用。容器外未知字段和已绑定插件非法值仍拒绝；卸载删除包 schema 投影，不留下影子 schema、参数账本或第二配置源。

受管服务绑定 SHALL 引用明确 package_id／plugin_id、service_id、已发布包版本及 manifest 允许范围；workspace 绑定另核精确工作区。Backend stdio 的 managed_launch／command 创建链 SHALL 迁齐为 Gateway 服务连接并删除，不保留新旧配置运行时双读。完整工作区运行时恢复所需非秘密绑定 SHALL 由 Workspace 配置 owner 重新认证交付；未交付时 Gateway 等待，不用旧实例／库存推断启用状态，不越界读取文件。Backend 单独重启／Gateway 正常接管时已核实实例仍沿原合同保留。服务 ready 前所需工作区输入 SHALL 由原 owner 经认证准入交付，缺失时明确等待输入而非伪报就绪。安装发布不自动提交配置或运行插件。

通用插件秘密参数 SHALL 沿 target-scoped-settings 的同一合同支持密码输入、保存后复用、更换及清除，由原 Gateway／Workspace 配置 owner 使用现有权限受限私有文件与原子发布保存不可变值；普通来源只保存绑定目标、必要 workspace、插件、参数位置和值版本的正式引用。writeOnly 允许专用受保护入口接收真实值，拒绝普通 JSONC／参数通道中的秘密明文，写入核原 configuration_write，不回显原值。引用更换与清除仍经原 source writer／CAS，秘密保存不代替配置提交或实例生效，不删除仍被 active／pending 引用的值，不新增秘密账本或通用 SecretStorage SDK。

既有 `${ENV_NAME}`／`env:NAME` SHALL 作为可选秘密来源，仅由配置所属目标的原 owner 解释；实际值按确切包版本 schema 验证，普通正文不自动展开引用。工作区秘密仅经原认证启动／配置应用交付传给对应插件，不进入 Gateway 接管材料或普通配置镜像；合法已保存输入在重建时复用，缺失和归属错误明确报告，不隐式改用其它目标或来源。状态读取／后台恢复不自动弹密码框。

认证交付 SHALL 由原启动／配置变更通知或用户显式重试驱动；缺输入报告 waiting，认证失败报告 failed，普通状态读取不重新交付或启动插件，不加轮询、扫盘及配置修复流程。

#### Scenario: 两个工作区绑定同一安装包
- **WHEN** A1/A2 在各自来源 CAS 提交同一插件包的使用关系和不同私有引用
- **THEN** 两者配置独立生效，Gateway 只管理合法登记与实例，工作区秘密仍由原 owner 保存；旧 command 不在 Backend 触发进程

#### Scenario: Gateway 启动时工作区输入尚未交付
- **WHEN** Gateway 已读取非秘密服务登记，但所需 workspace 输入尚未由其 owner 交付
- **THEN** 原状态明确等待输入，Gateway 不读取工作区文件、不发布业务 ready；控制通道可先可用，等待输入不阻塞 Backend 初始化或其它已就绪服务，输入到位后才核实业务启动，Backend 后续重启不顺带关闭已运行服务

### Requirement: 诊断热配置必须沿原 Gateway 配置域扩展

日志 SHALL 继续使用 `logging.runtime`；新增本地遥测／统计 SHALL 分别归 `telemetry.runtime`、`statistics.runtime`，自有源归 `plugins.sources`，复用 Gateway JSONC 族、schema、单父来源链与来源 writer，不增加插件配置副本。遥测／统计 enabled 默认 false，来源默认未配置；telemetry 的 sample_ratio 默认1且范围0–1，sample_buffer_limit 默认256且范围1–4096，statistics 的 collection_interval_ms 默认5000且范围1000–60000。schema SHALL 明确这些边界，允许原 Gateway 用户层合法覆盖，不接受 Workspace 覆盖。

只有已登记为热应用的字段 SHALL 在运行时应用；实例范围、入口或版本变更须明确对应实例的重启／切换，不由客户端猜测或一律重启 Backend。未支持的诊断能力 SHALL 明确报告，不将保存成功当作实际生效。

#### Scenario: 修改诊断开关不重启业务服务
- **WHEN** 用户在 Gateway 来源关闭遥测及统计并提交合法候选
- **THEN** 原 owner 以同一 revision 热应用并收集逐实例实际回执，成功实例停止额外采集，不更改工作区配置或为诊断开关重启 Backend

### Requirement: 插件开关和新建工作区默认值必须分离

当前 Gateway 的插件 enabled SHALL 归 Gateway 配置 owner，当前工作区的 enabled 归精确 Workspace owner；各有固定插件范围和包版本，不通过共享用户 Workspace 层 fan-out 修改其他工作区。开关和重启须获运行管理准入；同一来源写入同时遵守原配置写权限，不建立第二启用账本。实际状态单独报告，配置已保存不等于运行成功。

Gateway 新建工作区模板 SHALL 使用 plugins.workspace_defaults.entries，默认空数组，数组沿原整体替换语义；条目只有 package_id、plugin_id、确切 package_version 和 enabled，键组合唯一，只能引用已安装支持的 workspace 插件，不保存参数或秘密。沿 Gateway 实际单父链合法覆盖和 CAS 提交；不接受 Workspace 来源覆盖，模板编辑核原配置写权限，不产生进程副作用。模板和已冻结未结清创建 intent 的版本均为原安装 owner 的使用引用，更新清理及卸载不得删除；模板修改不解除旧 intent 引用，创建完成交接给工作区绑定，取消／失败明确结清才释放，不新建账本。安装不自动改模板；引用版本计入使用冲突，未解除引用不能卸载。

工作区创建 owner SHALL 在原创建 intent 冻结当时模板 revision 与条目，经原 Workspace writer 初始化该新工作区独立配置，再向 Gateway 交付已认证非秘密绑定；Gateway 不读写 .boxteam。原创建重试复用冻结输入，模板修改只影响之后新建工作区。已有工作区重新注册／挂接／启动不应用模板，不新增动态继承或第二创建账本。

模板提交、创建冻结／引用交接与包删除 SHALL 复用原安装互斥边界，交接完成后由工作区绑定接替 intent 引用，不先释放后创建引用，也不等对方先释放造成循环。卸载仅检查原实例、active／pending 绑定、模板／未结清 intent 和未完成读取／使用 lease；来源无法确认则拒绝该次卸载并说明，不新增全局引用追踪或后台巡检，不为检查启动 Backend。

#### Scenario: 当前工作区和以后新建默认值独立
- **WHEN** A1／A2 已存在，用户仅关闭 A1 的插件，再修改新建默认值并创建 A3
- **THEN** A1 按自身设置关闭，A2 不变，A3 取得创建时模板的独立设置；之后修改模板不改 A3，也不回写 A1

#### Scenario: 创建重试保持模板冻结
- **WHEN** 创建 intent 冻结模板 revision R 后中断，模板变为 R2，再重试原创建
- **THEN** 同一创建继续使用 R，只有后续新 intent 使用 R2；即使模板移除 R，其版本仍受原 intent 引用保护，更新清理／卸载返回该引用冲突，完成或取消后按原 owner 交接／释放；挂接已有工作区不写模板值，Gateway 无工作区文件访问

#### Scenario: 模板非法引用与版本卸载
- **WHEN** 用户保存不存在／非 workspace 插件或重复键的模板，或卸载当前模板引用的版本
- **THEN** 前者提交前拒绝且原模板保持，后者返回真实模板引用冲突，不自动安装、清空默认值或停其他插件
