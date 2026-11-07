## ADDED Requirements

### Requirement: MCP 候选规划与安装管理必须使用独立原生授权

原联邦 policy 与 Gateway schema SHALL 登记 `mcp_installation_manage`，覆盖 MCP 候选 manifest 读取、确切 variant 计划、消费前重验及后续制品安装管理。该管理能力 SHALL 默认拒绝，只有精确绑定已认证主体的显式 allow 生效；discovery、read、managed_workspace_admin、configuration_read 和 configuration_write 均不得代授。此能力不隐式授予配置写入、进程重启或宿主管理。

两种 workspace API mount SHALL 使用相同能力边界。Relay SHALL 透传冻结目标、原请求、原错误与同一 request_id，由原生 Gateway 在访问 Backend 前及实际披露前核现行授权；等待目标运行或读取响应期间撤权，不得继续准入或披露候选。无权限时 MUST NOT 请求 Backend、读取目录来源、下载制品、写配置或改投本地。

#### Scenario: 普通能力与通配 allow 不能规划 MCP 安装
- **WHEN** 远端主体只有发现、读取、配置或受管 Backend 管理能力，或安装 allow 未精确绑定该认证主体
- **THEN** 两种 mount 的候选原件、计划及重验请求均被原生 Gateway 拒绝，下游 Backend 与目录来源没有请求，Relay 不使用本机权限代授

#### Scenario: 候选请求等待或披露前撤销管理权限
- **WHEN** 精确主体获准的候选请求正在等待 Backend 或等待完整响应，原生 Gateway 撤销 mcp_installation_manage
- **THEN** 原生 Gateway 在下一准入或实际披露点拒绝，关闭已取得响应并结清本次路由引用，不返回冻结前的旧授权结果

### Requirement: 目标扩展安装与宿主管理使用独立联邦能力

原联邦 policy 与 Gateway schema SHALL 登记 `extension_installation_manage` 和 `extension_host_manage` 两项独立管理能力，且均归入管理员能力集合并默认拒绝。只有精确绑定已认证主体的显式 allow 生效；discovery、read、`managed_workspace_admin`、配置读写及另一项扩展管理能力均不得代授。`extension_installation_manage` 只授权明确目标上的 VSIX 制品安装、更新、移除及安装状态；`extension_host_manage` SHALL 统一授权该目标 Web／Node 扩展的运行准入、激活、运行状态、停止及适用的宿主生命周期／通道管理。Web 执行位置仍是当前浏览器，授权归安装目标，不以 Node 宿主可用作为 Web 运行前提。两种能力不得互相扩大制品写入与运行权限。

原默认 profile 的只读安装描述及包资源 SHALL 允许任一管理能力读取，包括入口脚本、静态贡献和相对资源；读取不授予运行或制品写入。资源 SHALL 属于明确目标、安装域、扩展及可访问版本：当前安装版本或仍合法运行的原实例冻结版本，不得凭任意历史版本地址访问保留包。原生 Gateway SHALL 在资源准入及实际披露时核现行授权和版本资格；旧实例须保有运行授权，关闭或运行撤权后不能借仍有的安装权限继续使用旧绑定。Relay 与浏览器缓存不得代授，也不新增第三项资源权限或授权账本。

共同目标发现 SHALL 从原生 Gateway 的原安装域登记及组件管理事实返回当前明确目标的稳定身份和两项管理能力判定；至少一项获准才访问其托管的目标管理服务，不以 Backend 活跃或工作区已挂接为前提，不披露未授权宿主运行态，也不由发现请求启动 Backend、宿主或管理进程。原生 Gateway SHALL 在请求准入及实际披露时重核权限、用户与路由，Relay 只转送结果，不合并本机或各分根的授权。客户端投影不代替后续请求的实时授权。

扩展安装及库存 SHALL 交目标原生 Gateway 托管的唯一常驻 Node 安装服务，复用现有组件生命周期与受权通信；VSIX 下载、配置文件 I/O、包资源字节读取与外部监听 SHALL 交目标侧原 Python 服务。原 Code OSS 配置语义、安装事实及必要 profile 描述各沿原 owner，不把 Node 暴露为通用基础设施。Relay 不在本机为远端启动管理副本或代读目标文件；管理服务的启动归 Gateway 服务组，不由安装权限、资源读取或单个 Backend 生命周期触发。此职责划分不改变既有读写／运行权限，也不将 MCP 或工作区业务配置 owner 迁入 Gateway。

经 Relay 发起的请求 SHALL 保留明确目标和原始错误，并由目标原生 Gateway 在访问 Backend、制品或宿主进程前按当前策略最终判权。未获授权时 MUST 明确拒绝，不访问目标资源，也不改投本机或其他目标。

#### Scenario: 普通能力或通配规则不能管理目标扩展
- **WHEN** 主体只有 discovery、read、`managed_workspace_admin`、配置能力，或管理 allow 没有精确绑定该认证主体
- **THEN** 目标原生 Gateway 在任何 Backend、制品或宿主访问前拒绝请求，Relay 不以本机或其它能力代授

#### Scenario: 安装管理与宿主管理彼此隔离
- **WHEN** 主体只有 `extension_installation_manage` 却请求激活 Web 扩展或启动、查询运行态、管理通道、停止 Node 宿主，或只有 `extension_host_manage` 却请求安装、更新或移除 VSIX
- **THEN** 目标原生 Gateway 拒绝缺少的对应能力，另一项能力不代授

#### Scenario: 单项管理权限独立初始化窗口
- **WHEN** 窗口明确挂接一个只有安装权限的目标与另一个只有宿主管理权限的目标
- **THEN** 两者通过同一目标发现取得自己的身份和权限；安装目标可以读取库存与包资源，但不请求宿主运行态或激活 Web／Node 实例；运行目标可以只读原 profile 描述及包资源，并按能力运行已安装的 Web／Node 扩展，但不能通过安装入口写入制品；同屏其它目标不被改投或隐式授权

#### Scenario: 只读包资源复用任一管理权限
- **WHEN** 已认证主体分别只有安装权限、只有运行权限或两项均无，通过原生入口或 Relay 读取当前安装包的入口脚本及相对资源
- **THEN** 前两种按同一资源 owner 和版本资格读取，均无权限时在目标资源披露前拒绝；安装权限不触发 Web 激活，运行权限不触发制品变更，Relay 不借本机权限或另一目标缓存代授

#### Scenario: Web 运行撤权使旧实例资源资格失效
- **WHEN** 主体的 Web v1 实例在更新后仍合法运行，原生 Gateway 撤销其 `extension_host_manage` 但保留 `extension_installation_manage`
- **THEN** 原运行绑定失效并沿原生命周期关闭，后续旧实例资源请求被拒绝，不因仍有安装权限、旧 URL 或浏览器缓存延续运行；其它仍获准实例的资源读取不被撤销

### Requirement: 远端配置管理必须使用独立读写能力

远端配置管理 SHALL 在原联邦 policy 注册 `configuration_read` 与 `configuration_write`，两者默认拒绝，仅精确绑定已认证主体的显式 allow 授权。discovery、普通 read 或 managed_workspace_admin 不代授配置能力；Relay 透传原请求、稳定目标、CAS／幂等身份及错误，由原生 Gateway 最终判权和配置 owner 执行，不保存可生效远端副本。

`configuration_read` SHALL 覆盖脱敏 schema／source／effective／pending／提交状态及事件披露；`configuration_write` SHALL 覆盖来源预检、候选创建／提交、删除覆盖和 pending retry／discard／resolve／失败记录。返回配置投影另核 read，读取不授写权限。长订阅在实际披露点使用现行策略；撤权停止后续披露或写入准入，已经接受的操作仍按原身份查询获准状态，不假报副作用已撤销。

实际受管 Backend 重启 SHALL 继续核原 `managed_workspace_admin`；消费配置 candidate 时另核 `configuration_write`，retry 不能代替真实重启或健康证明。Gateway 自身 `restart-development` 保持 local-only／源码环境限制，不新增联邦 Gateway 重启能力。

#### Scenario: 发现或普通读取权限不能写配置
- **WHEN** 已认证远端主体只有 discovery、read 或 managed_workspace_admin，尝试读取新配置来源入口或提交来源修改
- **THEN** 原生 Gateway 拒绝缺少的配置能力，目标没有来源写入或配置执行，Relay 不改投本地目标

#### Scenario: 配置读写独立且通配不授管理权
- **WHEN** 主体只有精确配置 read，或 policy 只有未绑定主体的配置管理 allow，尝试预检／提交
- **THEN** 缺少 write 或精确认证主体时拒绝且无写入；只有 write 的请求也不能取得未经 read 授权的配置投影

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
