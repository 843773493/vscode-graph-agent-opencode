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

原联邦 policy 与 Gateway schema SHALL 登记 `extension_installation_manage` 和 `extension_host_manage` 两项独立管理能力，且均归入管理员能力集合并默认拒绝。只有精确绑定已认证主体的显式 allow 生效；discovery、read、`managed_workspace_admin`、配置读写及另一项扩展管理能力均不得代授。`extension_installation_manage` 只授权明确目标上的 VSIX 制品安装、更新、移除及安装状态；`extension_host_manage` 只授权该目标 Node Extension Host 的启动、通道管理、状态读取及停止。两种能力不得互相扩大权限。

经 Relay 发起的请求 SHALL 保留明确目标和原始错误，并由目标原生 Gateway 在访问 Backend、制品或宿主进程前按当前策略最终判权。未获授权时 MUST 明确拒绝，不访问目标资源，也不改投本机或其他目标。

#### Scenario: 普通能力或通配规则不能管理目标扩展
- **WHEN** 主体只有 discovery、read、`managed_workspace_admin`、配置能力，或管理 allow 没有精确绑定该认证主体
- **THEN** 目标原生 Gateway 在任何 Backend、制品或宿主访问前拒绝请求，Relay 不以本机或其它能力代授

#### Scenario: 安装管理与宿主管理彼此隔离
- **WHEN** 主体只有 `extension_installation_manage` 却请求启动、查询运行态、管理通道或停止 Node 宿主，或只有 `extension_host_manage` 却请求安装、更新或移除 VSIX
- **THEN** 目标原生 Gateway 拒绝缺少的对应能力，另一项能力不代授

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
