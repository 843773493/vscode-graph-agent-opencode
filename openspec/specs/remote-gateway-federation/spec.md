## Purpose

定义经已认证 SSH 会话建立的 Gateway 联邦，包括单一控制端口隧道、远程工作区投影和代理、后端生命周期委托、独立凭据认证、协议兼容检查，以及禁止循环和嵌套导出的有界拓扑。
## Requirements
### Requirement: SSH 连接远程 Gateway
SSH 远程连接 SHALL（必须）只转发远程 Gateway 控制端点，MUST NOT（不得）直接转发远程 Workspace API、Terminal Manager 或 Browser Manager 端点。

#### Scenario: 建立远程连接
- **WHEN** 用户通过 SSH 连接远程 BoxTeam 安装
- **THEN** 本地 Gateway 在导入远程 Gateway 直接管理的工作区之前验证其协议

### Requirement: 联邦工作区投影

本地 Gateway SHALL（必须）以来源原生 Gateway 的稳定 `gateway_id` 和工作区自身 UUID `workspace_id` 表示远程工作区，并 SHALL（必须）通过远程 Gateway 路由业务请求。投影 MUST 保留来源 UUID，不得生成本地 `gw_...` 工作区 ID 或维护 `remote_workspace_id` 换算。`connection_id` 只标识连接；同一来源工作区经不同连接导入时不得改变其资源身份，不同 Gateway 暴露相同 UUID 时不得合并业务状态。

#### Scenario: 请求远程工作区
- **WHEN** 当前活动工作区属于远程 Gateway
- **THEN** 本地 Gateway 携带同一个工作区 UUID 和明确目标 Gateway 归属向该 Gateway 转发请求，并保留原 request ID

#### Scenario: 相同 UUID 来自不同 Gateway
- **WHEN** 发现两个来源 Gateway 各自直接管理相同 UUID 的工作区
- **THEN** 投影、活动选择及代理按来源 Gateway 区分两者，不生成本地工作区别名

### Requirement: 委托后端生命周期
本地 Gateway SHALL（必须）把远程工作区的重启和状态操作委托给拥有该工作区的远程 Gateway。

#### Scenario: 重启远程托管后端
- **WHEN** 用户请求安全重启由远程 Gateway 管理的工作区
- **THEN** 本地 Gateway 转发该操作，并展示远程排空结果和 blockers

#### Scenario: 远程工作区不受托管
- **WHEN** 远程 Gateway 报告目标后端属于外部管理
- **THEN** 系统拒绝重启请求，只提供健康探测

### Requirement: 联邦认证
Gateway-to-Gateway 调用 MUST（必须）使用与浏览器本地认证分离的凭据；凭据作用域限于已配对的远程 Gateway，存放在工作区业务数据之外，并且不得出现在日志和 API DTO 中。

#### Scenario: 通过 SSH 认证配对
- **WHEN** 创建新的远程 Gateway 连接
- **THEN** 已认证的 SSH 会话获得短期、限定作用域的凭据，且不向浏览器暴露该凭据

#### Scenario: 联邦凭据无效
- **WHEN** 远程请求携带无效或已过期的联邦凭据
- **THEN** 远程 Gateway 拒绝请求，且不回退使用本地开发凭据

### Requirement: 协议兼容与有界联邦
Gateway 之间 MUST（必须）交换协议版本，并 SHALL（必须）拒绝不兼容版本、联邦循环，以及本身由其他 Gateway 导入的工作区。

#### Scenario: 远程 Gateway 不兼容
- **WHEN** 不支持远程协议版本
- **THEN** 连接失败，并报告本地和远程协议版本

#### Scenario: 远程 Gateway 暴露嵌套远程工作区
- **WHEN** 工作区发现返回并非由该远程 Gateway 直接拥有的工作区
- **THEN** 本地 Gateway 排除该工作区，并报告有界联邦规则

### Requirement: 重连失败可观察
远程隧道或 Gateway 恢复失败 MUST（必须）持续显示在控制面状态中，并 MUST NOT（不得）静默切换到其他工作区。

#### Scenario: 重启后远程 Gateway 不可用
- **WHEN** Gateway 无法恢复已持久化的远程连接
- **THEN** Gateway 保留连接记录并写入明确错误，同时保持工作区激活状态不变

### Requirement: 联邦工作区可参与本地目录与生成器配置

本地 Gateway SHALL 允许工作区文件夹、目录搜索和 GeneratorDefinition 引用其直接导入的远程工作区投影，并 MUST 使用来源 `gateway_id` 与原工作区 UUID 路由到远程 Gateway。持久引用不得保存本地投影别名或以 `connection_id` 代替工作区身份；连接更新后只要来源归属不变，原引用必须继续有效。

#### Scenario: 生成器挂载远程会话
- **WHEN** 用户把生成器挂载到直接导入的远程工作区会话
- **THEN** 本地 Gateway 通过远程 Gateway 代理校验和执行，并在远程离线时保留配置且标记 blocked/offline

#### Scenario: 远程连接参数更新
- **WHEN** 同一来源 Gateway 的连接完成受控 generation 更新
- **THEN** 已有目录和生成器引用继续使用原 Gateway 归属与工作区 UUID，无需改写成新工作区 ID

### Requirement: 禁止嵌套联邦生成路由
本地 Gateway MUST NOT 通过远程 Gateway 继续引用其导入的嵌套远程工作区作为生成或目录目标。

#### Scenario: 远程目录快照包含嵌套工作区
- **WHEN** 远程 Gateway 返回非其直接拥有的工作区目录能力
- **THEN** 本地 Gateway 排除该目标并报告有界联邦错误

### Requirement: 联邦配置变化必须由拥有者应用

本地 Gateway SHALL（必须）只监听和应用自己的 Gateway 配置。涉及远程 Gateway 的配置、重启或运行时生命周期变化 MUST（必须）发送给拥有该目标的远程 Gateway 处理；本地 Gateway 只能更新远程工作区投影和展示远程应用结果，不得直接写入远程工作区或绕过远程生命周期。

远程 Workspace 的 pending candidate SHALL（必须）由远程 Workspace 配置域保存和读取。委托链只能传递绑定远程 Workspace/config domain/candidate 的不透明 candidate_ref，并接收远程 generation health proof、promotion 或 recovery 结果；本地 Gateway 不得读取远程或本地 Workspace `.boxteam/` SQLite，也不得把候选 payload 或秘密复制到本地 registry/projection。

#### Scenario: 修改本地 Gateway 配置中的远程目标
- **WHEN** 用户修改本地 Gateway 中远程 Gateway 的连接参数
- **THEN** 本地 Gateway 在本地配置候选中校验并重建该联邦连接，保留其他远程目标不变

#### Scenario: 远程配置要求重启
- **WHEN** 远程 Gateway 报告其配置变化需要重启
- **THEN** 本地 Gateway 展示远程 pending/restart 状态，并把重启操作委托给远程 Gateway

#### Scenario: 远程 Gateway 不可用
- **WHEN** 配置重载期间远程 Gateway 无法连接
- **THEN** 本地 Gateway 保留配置和工作区投影，报告 offline/blocked，不静默切换到其他目标

#### Scenario: 远程 pending 委托
- **WHEN** 远程 Workspace 需要以 pending candidate 重启 backend
- **THEN** 本地只转发不透明 candidate_ref 并等待远程 proof；远程返回的 proof 必须匹配 candidate/revision/digest，本地不能用普通 HTTP healthy 或本地数据库状态代替

### Requirement: 联邦资源状态必须区分投影所有权

本地 registry 中的 `remote_projection` SHALL（必须）保存来源 Gateway 标识、工作区自身 UUID、连接关联、来源 event cursor 和当前 runtime lease。工作区身份 MUST 是 `(gateway_id, workspace_id)`，owner 类型和连接 ID 不得成为第二套工作区定位关系。远程投影不得被本地 `config` 对账删除或改写；只有来源 Gateway 的有序配置/生命周期事件才能改变其投影，事件断档时必须先重新获取快照再对账。

Federation manifest SHALL（必须）返回 `config_event_cursor`、`config_reload_state`、`config_reload_restart_required` 和可选的不透明 `config_reload_candidate_ref`。本地只保存 cursor、状态摘要和委托句柄，不得保存远端候选 payload、secret 或 proof 原文。完整 workspace 快照是断档恢复边界：较旧 cursor 必须拒绝，跳跃 cursor 只有在完整快照成功校验并通过本地 registry CAS 后才能持久化；失败时保留旧 projection、旧 cursor 和 runtime lease。

#### Scenario: 本地配置删除远程投影同名目标
- **WHEN** 本地 Gateway 配置删除一个与远程投影显示名相同的 `config` 目标
- **THEN** 对账依据工作区归属、连接关系和 owner 类型分别处理，不能删除远程投影

#### Scenario: 远程事件 cursor 断档
- **WHEN** 本地 Gateway 收到的远程配置事件 cursor 不连续
- **THEN** 本地暂停该投影的破坏性对账，要求远程快照重同步，期间保留旧投影和租约

#### Scenario: 远程快照游标回退
- **WHEN** 重连后的远程 Gateway 返回小于本地已确认 cursor 的 workspace 快照
- **THEN** 本地拒绝该快照，不替换 projection、runtime lease 或已确认的 cursor

#### Scenario: 远程快照跨越多个事件
- **WHEN** 远程 Gateway 返回大于本地 cursor 的完整 workspace 快照
- **THEN** 本地将其作为断档后的快照重同步，完整 CAS 成功后一次性更新 projection 和 cursor

#### Scenario: 远程隧道仍被使用
- **WHEN** 远程 Gateway 请求删除或替换一个仍有本地代理 lease 的目标
- **THEN** 本地先报告真实 blockers 并完成排空，未完成时保留隧道和投影，不提前关闭连接

### Requirement: 自有插件安装与运行管理必须分别由原生目标授权

原联邦 policy 与 Gateway schema SHALL 登记 `service_plugin_installation_manage` 和 `service_plugin_runtime_manage`，均属于管理能力、默认拒绝，只有精确绑定已认证主体的显式 allow 生效。前者覆盖自有包来源／计划、安装／整体更新／卸载及对应管理记录；后者覆盖明确插件绑定的开启／关闭／重启、运行状态和管理通道，不提供按需启动授权流程。普通 discovery／read、配置读写、managed_workspace_admin 及另一项管理能力不得代授。

实际插件业务调用 SHALL 继续核原工作区／资源权限与精确服务范围；运行管理权限不授予任意工作区文件、工具或秘密访问，安装权限也不自动启用／运行服务。已认证 Backend 的服务请求 SHALL 由原登记身份映射工作区，不信任未验证的调用方自报归属。旧 `extension_installation_manage`、`extension_host_manage` 和 `mcp_installation_manage` 的新体系消费者 SHALL 迁齐后物理删除；支持基线需要升级的授权经显式原 owner 迁移，不运行时互认／合并权限。

原生目标 SHALL 在实际资源准入和披露点最终判权，Relay 只传原请求、明确目标及同一 request_id，不借本机凭据代授，不读取／复制远端安装事实、工作区秘密或业务状态。长连接保留原授权与撤权边界；撤权不假报已经发生的副作用被回滚。

自有贡献清单、schema 和状态读取 SHALL 沿其原披露权限执行，不以 runtime 已启动或运行管理权限为前提；命令调用另核其声明的业务能力及实际资源 owner，未绑定合法权限的命令明确拒绝，不以可见菜单或插件安装资格放行。插件开启／关闭会持久修改 enabled，原生目标 SHALL 在配置提交及运行副作用前同时核验 service_plugin_runtime_manage 和 configuration_write，任缺其一均拒绝且不写来源；只重启当前绑定、不写配置时仅按运行管理及原业务边界核验。自动恢复只消费已提交配置和原受管身份，不伪造用户配置提交。已开启插件的普通业务调用不要求启停权限，也不触发启动。具名命令不是任意 shell 或 Gateway 管理能力的别名。

包内 Skill 的发现及配套资源读取 SHALL 沿原 Skill 来源和原生目标资源权限核验，启用范围限定到明确绑定；安装或运行管理资格不代授工作区 Skill／工具访问，读取 Skill 也不授予工具执行或进程启动。Relay 不复制目标技能目录，不将同名本地 Skill 当作远端副本；撤权后撤销对应的新读取／加载及观察准入，保留原会话已封存历史。

#### Scenario: 安装管理与运行管理独立
- **WHEN** 主体只有安装管理却请求启动／重启，或只有运行管理却请求安装／卸载，或只有配置能力访问两者
- **THEN** 原生 Gateway 在进程和制品副作用前拒绝缺少的能力，Relay 不代授，旧管理能力也不隐式放行

#### Scenario: 插件开关必须同时取得配置和运行管理授权
- **WHEN** 主体只有 configuration_write 或只有 service_plugin_runtime_manage，尝试修改插件 enabled
- **THEN** 原生目标在来源写入及进程动作前拒绝，enabled 和实例均不改变；仅重启不写配置另按运行管理授权执行

#### Scenario: 业务请求不能伪造工作区
- **WHEN** Backend 或用户请求 Gateway 插件业务路由，声明另一个没有访问资格的工作区或资源
- **THEN** 原身份／资源 owner 拒绝，不因服务是 gateway 单实例而共享该工作区的终端、浏览器或 MCP 凭据

#### Scenario: 经 Relay 调用插件命令不继承安装权限
- **WHEN** A 的主体可查看或安装 B 的插件，但缺少该命令的业务能力，经命令面板向 B1 发起调用
- **THEN** B 在实际命令执行前拒绝；A 不凭本机主体代授，菜单可见和 runtime 已 ready 也不造成业务副作用

#### Scenario: 远端插件关闭与普通调用权限分离
- **WHEN** 主体可调用 B1 已开启插件的业务但无运行管理权限，另一个获准用户关闭该插件
- **THEN** 关闭前业务按原权限可用，关闭后新调用明确不可用且零自动启动；关闭只影响 B1 精确插件，A／B2 和兄弟插件不变

### Requirement: 远端插件管理与业务通道必须抵达原生实例

A Gateway SHALL 沿既有认证联邦路由将目标 B 的插件管理、HTTP／SSE／WebSocket 请求转到 B 原生 Gateway，再由 B 解析原 package_id／plugin_id／service_id／workspace／instance_id。路由在请求／连接开始时冻结，在途连接不随活动工作区或 UI 目标变化改投；旧实例失效、路由撤销或断连 SHALL 保留原错误和结果未知，不自动重发写入。

B 的实例创建、进程日志与退出核实 SHALL 由 B 原进程 owner 执行。目标发现和安装库存不以 Backend 活跃或 workspace 已挂接为前提，也不隐式启动插件；无来源时只披露自有源未配置状态。Relay 不在 A 启动 B 的服务副本、不把下一跳连接选择当作本跳身份。

#### Scenario: 从 A 管理 B 与直接访问 B 一致
- **WHEN** 两个页面分别经 A 访问 B 和直接访问 B，读取并使用同一服务
- **THEN** 两者观察 B 的同一安装和运行实例，操作均在 B 发生；B 离线不回退 A，切列表目标不改变已连接流

#### Scenario: 写入响应丢失不经 Relay 重放
- **WHEN** B 已收到安装、终端命令或浏览器写操作但 A 未取得完整响应
- **THEN** 保留原请求／操作身份及实际未知状态，已有查询合同可查原状态，Relay 不更换幂等身份或自动发送到新实例

### Requirement: 远端配置管理必须使用独立读写能力

远端配置管理 SHALL 在原联邦 policy 注册 `configuration_read` 与 `configuration_write`，两者默认拒绝，仅精确绑定已认证主体的显式 allow 授权。discovery、普通 read 或 managed_workspace_admin 不代授配置能力；Relay 透传原请求、稳定目标、CAS／幂等身份及错误，由原生 Gateway 最终判权和配置 owner 执行，不保存可生效远端副本。

`configuration_read` SHALL 覆盖脱敏 schema／source／effective／pending／提交状态及事件披露；`configuration_write` SHALL 覆盖来源预检、候选创建／提交、删除覆盖和 pending retry／discard／resolve／失败记录，以及所属目标插件参数的受保护秘密写入、更换与引用清除。秘密写入只返回引用，不因 read／write 或安装／运行权限提供原值回读；Relay 仅认证转送至冻结的原 owner，不保存秘密或在本机解析其环境引用。返回配置投影另核 read，读取不授写权限。长订阅在实际披露点使用现行策略；撤权停止后续披露或写入准入，已经接受的操作仍按原身份查询获准状态，不假报副作用已撤销。

实际受管 Backend 重启 SHALL 继续核原 `managed_workspace_admin`；消费配置 candidate 时另核 `configuration_write`，retry 不能代替真实重启或健康证明。Gateway 自身 `restart-development` 保持 local-only／源码环境限制，不新增联邦 Gateway 重启能力。

#### Scenario: 发现或普通读取权限不能写配置
- **WHEN** 已认证远端主体只有 discovery、read 或 managed_workspace_admin，尝试读取新配置来源入口或提交来源修改
- **THEN** 原生 Gateway 拒绝缺少的配置能力，目标没有来源写入或配置执行，Relay 不改投本地目标

#### Scenario: 配置读写独立且通配不授管理权
- **WHEN** 主体只有精确配置 read，或 policy 只有未绑定主体的配置管理 allow，尝试预检／提交
- **THEN** 缺少 write 或精确认证主体时拒绝且无写入；只有 write 的请求也不能取得未经 read 授权的配置投影

#### Scenario: 经 Relay 保存插件秘密仍由目标 owner 判权
- **WHEN** A 为 B1 插件提交密码输入，主体只有 configuration_read 或只有安装／运行管理权限；随后获 configuration_write 再提交
- **THEN** 前者在秘密写入前拒绝，获准后仅 B1 原 owner 保存并返回绑定原目标的引用；普通响应不含原值，A 不持久化副本，切换界面目标不改变在途写入归属

#### Scenario: 订阅期间撤销读取授权
- **WHEN** 主体已订阅配置事件，原生 Gateway 撤销其 configuration_read 后产生新配置事件
- **THEN** 后续事件与重放内容不再披露给该主体，不以订阅建立时的旧快照继续授权

#### Scenario: 配置权限不授予进程重启
- **WHEN** 主体具有 configuration_read／configuration_write 而没有 managed_workspace_admin，执行 pending retry 并请求实际 Backend 重启或远端 Gateway 开发栈重启
- **THEN** retry 只处理原候选状态；Backend 重启被拒，远端 Gateway 开发栈重启仍不可用，不伪造健康证明

#### Scenario: 原生 Gateway 来源经 Relay 读取和保存
- **WHEN** 用户通过 A 为明确的目标 B 读取来源列表／正文、预检或保存，并在断流后用原幂等键查询
- **THEN** A 只使用原 registry 中属于 B 的明确连接和登记凭据，B 沿原来源 owner 判权并执行；保留 B 的状态、错误、CAS、提交身份及同一请求的 request_id，A 的来源文件和 active 不被替代修改，不另建配置镜像或自动重放保存

#### Scenario: 配置 Relay 目标和连接必须一致
- **WHEN** 显式目标没有合法路由、连接归属不同、多个连接尚未选择，或已认证联邦请求要求再借当前 Gateway 凭据转发
- **THEN** 在来源读取或写入前明确拒绝，不选择首项、活动工作区或本机作为替代目标；旧连接仍有在途请求时保留其跟踪并在本次请求结束后结清引用

#### Scenario: 原生 Gateway pending 管理沿原状态机执行
- **WHEN** 用户经 A 查询 B 的配置状态，或以原 candidate_ref、证明及 active 基线请求 retry／resolve／discard
- **THEN** 由 B 核配置读写能力并执行原 CAS／fencing／证明校验，原状态及冲突回到客户端；A 的 active／pending 不改变，retry 不被解释为实际 Gateway 重启

#### Scenario: 配置事件 Relay 保留游标与撤权边界
- **WHEN** 用户经 A 为 B 以 after 或 Last-Event-ID 续读事件，期间 B 撤销 configuration_read，或客户端在响应头及正文阶段断开
- **THEN** B 按原规则拒绝非法／冲突／过期游标，已建立的流透传原事件身份并在撤权后以原 error 终止；A 不另建事件消费事实或自动续连，开放响应及客户端真正关闭后才结清该连接引用，未开始的正文也执行同一关闭链
