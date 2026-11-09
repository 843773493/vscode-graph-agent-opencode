## ADDED Requirements

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
