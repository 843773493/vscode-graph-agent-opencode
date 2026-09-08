## ADDED Requirements

### Requirement: Workspace 配置按生效范围分类

Workspace SHALL（必须）为有效配置路径声明可观察的生效范围。模型、Agent 和开发运行参数的变化 MUST（必须）至少对后续 Job 生效；默认 Agent 的变化 MUST（必须）只影响后续新会话；UI 和按请求读取的配置变化 MUST（必须）对后续读取生效；当前 Job、当前会话的既有绑定和已经建立的请求 MUST（必须）保持原语义。

#### Scenario: 修改模型或 Agent 配置
- **WHEN** 有效 `llm` 或 `agents` 配置热重载成功
- **THEN** 后续 Job 使用新配置，正在运行的 Job 不被替换或重建

#### Scenario: 修改默认 Agent
- **WHEN** 有效 `default_agent` 发生变化
- **THEN** 新建会话使用新的默认 Agent，已有会话继续使用其已持久化的 Agent 标识

#### Scenario: 修改已被启动时缓存的运行时依赖
- **WHEN** 修改日志、MCP、Agent 超时或辅助服务/Gateway 连接地址等不能由当前进程安全替换的配置
- **THEN** 系统报告 Workspace backend restart required，并不得声称该配置已经即时生效

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

Gateway registry 中的每个目标 SHALL（必须）具有不可变目标标识、唯一 owner 类型和当前 runtime lease。owner 类型至少包括 `config`、`manual`、`system` 和 `remote_projection`；配置对账只允许创建、更新和移除 `config` 目标，MUST NOT（不得）删除其他 owner 的目标。关闭 SSH 隧道或服务前 MUST（必须）确认没有活动 lease、代理请求或 SSE/WebSocket 使用者，并完成排空。

#### Scenario: 配置删除与手动目标并存
- **WHEN** 新的 Gateway 配置删除一个曾由配置创建的目标，同时 registry 中存在手动目标
- **THEN** 对账只移除对应 `config` 目标，保留 `manual`、`system` 和 `remote_projection` 目标

#### Scenario: 目标仍有活动租约
- **WHEN** 配置更新要求替换一个仍被代理请求或事件流使用的目标
- **THEN** 系统先保留旧 generation 并等待排空；排空失败时保留旧目标和 active revision，不提前关闭隧道

#### Scenario: 远程投影返回删除
- **WHEN** 远程 Gateway 的投影事件声明某个远程工作区已删除
- **THEN** 本地只能按远程投影 owner 和事件 revision 移除该投影，不得把它当作本地 `config` 目标处理

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

Workspace SHALL（必须）分别维护 `user`、`user_local`、`workspace` 和 runtime override 的 payload、`layer_revision`、`layer_digest`、来源路径和 CAS 基线，不得把 `workspace.jsonc` 与 `workspace_local.jsonc` 合并成一个来源层。有效配置仍按既有优先级合并；source layer 更新不等于 active snapshot 更新。用户级 `workspace.jsonc` 是一个共享 source，写入或 watcher 导入必须生成 `fanout_id`；显式 source writer 直接分配该关联键，直接人工编辑则由 watcher 按规范化 source path 与 post-write layer digest 派生。该键只用于 fan-out 关联/幂等，不得充当 commit revision、event cursor 或内容 digest。

每个 Workspace 必须独立执行 layer/active CAS，并分别分配自己的 candidate_id、commit_revision 和配置事件。运行中的 Workspace 立即尝试导入，未运行的 Workspace 在下次启动时追赶同一 source generation；跨 Workspace 不得伪造一个 SQLite 全局事务。一个 Workspace 冲突不得回滚其他 Workspace 的成功导入，跨 Workspace 汇总结果可报告为 `fanout_partial`，但该名称只属于 fan-out 诊断汇总，不属于单 Workspace 配置事件的 `result` 枚举。被更高优先级层遮蔽的 source layer 仍更新自身 layer revision/digest，但 effective digest 不变并返回 `unchanged`。

#### Scenario: 用户级 Workspace JSONC fan-out
- **WHEN** 用户修改一次 `workspace.jsonc`，且多个 Workspace 中有的正在运行、有的已停止
- **THEN** 每个 Workspace 按同一 `fanout_id` 独立导入并产生自己的结果；运行中的立即处理，停止的在启动时追赶，任何一个 Workspace 的 commit_revision 都不冒充其他 Workspace 的 revision

#### Scenario: fan-out 中单个 Workspace 冲突
- **WHEN** 一个 Workspace 的 `user` layer 已被 API/UI 以更高 revision 修改，随后共享 `workspace.jsonc` 以旧基线到达
- **THEN** 该 Workspace 进入 `conflict` 并保留自己的 active/pending，其他基于各自有效基线的 Workspace 可继续导入；汇总诊断报告 `fanout_partial` 和逐 Workspace 结果

#### Scenario: API/UI 指定配置范围
- **WHEN** API/UI 修改 Workspace 配置
- **THEN** 请求必须明确是单 Workspace 的 `workspace`/`user_local`/runtime override，或是用户级 source；单 Workspace 请求只写目标 SQLite，用户级 source 请求先原子写 JSONC 再触发 fan-out，不能隐式改写其他层或多个 SQLite

### Requirement: Gateway workspaces 配置元素必须使用稳定身份

Gateway `workspaces` 配置数组的每个元素 SHALL（必须）包含持久的不可变 `connection_id`，并以该字段作为配置元素及 registry `config` target 的身份。新 Gateway 配置版本必须将 `connection_id` 纳入 schema；实现 MUST NOT（不得）使用数组下标、host、username、key 或可变连接参数推导身份。旧配置版本（当前为 `config_version: 1`）缺少该字段时，迁移 MUST（必须）在一次显式、可恢复的版本迁移中为每个无歧义元素生成并持久化 ID；重复、无法匹配或迁移写入失败必须拒绝，不得每次启动重新生成。

保留同一 `connection_id` 的 host、username 或 key 变化 SHALL（必须）视为同一目标的 generation 更新：新连接健康前保留旧 generation，健康后等待活动 lease、代理请求和 SSE/WebSocket 排空，再转移并关闭旧 generation。只有删除整个配置元素才允许删除对应 `config` target；`manual`、`system` 和 `remote_projection` target 不受该对账删除。

#### Scenario: 连接参数变化
- **WHEN** 一个 `workspaces` 元素保持 `connection_id` 不变但修改 host、username 或 key
- **THEN** registry 原地更新同一目标的 generation，先建立并确认新 tunnel，再排空旧 tunnel；不得表现为无关联的删除和新增

#### Scenario: 配置元素删除
- **WHEN** 一个带 `connection_id` 的 `workspaces` 元素从有效配置中删除
- **THEN** 只移除对应 owner=`config` 的 target，且在 lease/活动请求未排空前保留旧 generation；其他 owner 的同名或相似 target 不受影响

#### Scenario: 旧配置迁移与重复身份
- **WHEN** 旧版本数组元素没有 `connection_id`，或迁移后两个元素使用同一 ID
- **THEN** 首次迁移持久化一次性生成的 ID；重复 ID 或不确定映射使迁移失败并保留旧配置，不使用数组下标兜底

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

候选开始 apply 时保存的 source baseline 只用于准备阶段，不能作为最终 promotion 的充分条件。promotion 前，系统 MUST（必须）在同一事务中重新读取配置域全部 source layer 的 presence、layer revision、layer digest、source generation，以及该域当前 active/pending revision 和 Gateway registry revision（适用时），并对所有基线执行 CAS。任何一层发生变化都必须阻止 active promotion；不能只校验发生变化的路径或单一 source layer。

#### Scenario: 外部副作用期间 source 发生变化
- **WHEN** 候选 A 基于 source revision 1 开始 apply，候选 B 将任一 source layer 更新到 revision 2
- **THEN** 候选 A 的最终 CAS 失败，A 不得 promotion active、递增 active revision 或发布 applied paths，系统保留 B 的 desired state 并安排重建/冲突处理

#### Scenario: 最终 CAS 失败前尚未产生副作用
- **WHEN** promotion 前发现任一 source baseline、active revision 或 registry revision 不匹配，且外部资源尚未切换
- **THEN** 系统只终止 A 的 apply claim 并标记 conflict/rebuild_required，不执行破坏性补偿，也不改变 active snapshot

#### Scenario: 最终 CAS 失败但已有外部副作用
- **WHEN** source 在外部 apply 期间变化，且部分 tunnel、process 或消费者已经切换
- **THEN** 系统按 apply journal 执行补偿；补偿不完整时进入 recovery_required，不能报告 A 已应用或静默覆盖 B

### Requirement: Registry reconcile、manual CRUD 和 active promotion 必须使用同一 revision CAS

Gateway registry SHALL（必须）维护单调递增的 registry revision 和可恢复的 apply journal。config batch、manual CRUD、system 更新、remote projection 与 active promotion MUST（必须）声明 expected registry revision，并在同一 SQLite 事务中以 revision CAS 获取写入权；不得通过整表删除重插入绕过 revision 或覆盖其他 owner。registry target key MUST（必须）绑定 gateway identity、owner 和 target namespace；同一 `connection_id` 只在明确的 config owner 命名空间中唯一。

`mutation owner` 与 target 的不可变 owner 必须分开校验。system recovery 可以更新已存在 target 的运行时状态，但必须保持 target owner、稳定 identity 和 namespace 不变；system 不得借此创建、删除或改写其他 owner 的配置目标。该更新必须继续遵守 registry revision CAS 并写入 system apply journal。

system default target 的生命周期字段仍归 system 所有；为保留用户对默认 Workspace 的导航体验，manual CRUD 只允许修改该 target 的名称和父级组织，且必须保持 owner、stable identity、root、backend、managed 和 removable 不变。

#### Scenario: manual CRUD 与 config batch 并发
- **WHEN** config batch 已基于 registry revision N claim，manual CRUD 先提交 revision N+1
- **THEN** config batch 的最终 CAS 失败并报告 conflict，不能删除或覆盖 manual target；已产生的外部副作用按 journal 补偿

#### Scenario: config batch 先提交
- **WHEN** manual CRUD 使用旧 registry revision 提交，而 config batch 已提交新的 owner target
- **THEN** manual CRUD 被拒绝并返回当前 revision 和冲突 target，不能用旧整表快照覆盖 config batch

#### Scenario: system recovery 更新人工托管目标运行时状态
- **WHEN** Gateway 启动恢复以 mutation owner=system 更新一个已有的 owner=manual 托管目标的 backend 地址或 connection_error
- **THEN** 更新保持 target owner、稳定 identity 和 namespace 不变，并以 registry revision CAS/journal 提交；system 不能借此删除、创建或改写人工目标的所有权

#### Scenario: owner 名称相同但命名空间不同
- **WHEN** manual、config 和 remote projection 使用相同显示名称或远端 target id
- **THEN** registry 以 gateway/owner/namespace/immutable target identity 区分它们，对账删除只作用于 config owner
