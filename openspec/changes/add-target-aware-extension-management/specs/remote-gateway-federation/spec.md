## ADDED Requirements

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
