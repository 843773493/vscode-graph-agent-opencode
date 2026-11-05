## MODIFIED Requirements

### Requirement: Gateway 拥有所有本地托管工作区

Gateway SHALL（必须）拥有默认本地工作区 runtime 和每个新增的本地托管工作区 runtime；显式声明为外部管理的本地后端 SHALL（必须）保持仅可探测。一个本地托管 Backend MAY 挂载多个工作区，但其进程句柄、generation 和完整 mount 清单 MUST 由一个 Gateway managed Backend owner 唯一持有。工作区成员 MUST 保留自己的身份、业务数据、路由、运行意图与辅助服务，只引用该 owner，MUST NOT 各自持有或关闭同一 Backend 句柄。

共享关系 MUST 来自 Gateway 控制面显式登记的 owner 成员集合与 generation 启动合同；MUST NOT 根据相同 URL、端口、根目录相似性或所有 `managed && desired_running` target 猜测共享。持久化启动合同只包含每个成员的工作区 UUID、owner 归属和 generation，不保存物理根或另一份 Gateway 工作区 ID。负责原生工作区的 Gateway 唯一 registry 按工作区 UUID 持有 root binding；启动时 owner 从该 binding 组装完整 mount 输入。Backend 的已挂载工作区注册表只是该 generation 的内存 runtime view，不成为持久化根来源。Gateway 只管理控制面合同，MUST NOT 直接读取工作区业务数据。

#### Scenario: 默认工作区启动
- **WHEN** Gateway 使用有效默认工作区启动
- **THEN** Gateway 启动该工作区、以其自身 UUID 将其注册为托管工作区，并提供后端重启操作

#### Scenario: 外部本地后端
- **WHEN** 工作区指向由用户管理的本地后端
- **THEN** Gateway 提供健康探测，但拒绝重启其进程

#### Scenario: 一个托管后端挂载多个工作区
- **WHEN** 一个托管后端进程按注册表挂载多个工作区
- **THEN** 每个工作区的业务身份独立，同一 Backend 的进程句柄只属于一个 owner；成员关闭不得提前关闭仍服务 sibling 的进程

#### Scenario: 普通新增工作区
- **WHEN** 用户按现有入口新增未显式指定共享关系的本地托管工作区
- **THEN** Gateway MUST 保持原独立 Backend 启动行为，创建 singleton owner，MUST NOT 自动并入已有共享组或无故改成未运行

#### Scenario: 相同 URL 不证明共享所有权
- **WHEN** 外部管理的本地后端与托管 target 使用相同 URL
- **THEN** 外部 target MUST 仍只可探测/转发，不成为 managed owner 的成员，Gateway MUST NOT 创建、重启或关闭其进程

## ADDED Requirements

### Requirement: Backend 生命周期中的成员引用必须使用统一工作区身份

共享 Backend 的成员集合、blockers、完整影响集合、force 确认、配置 fan-out、preserve/adopt 和 startup recovery SHALL 使用工作区自身 UUID；跨 Gateway 委托 MUST 同时保留原生 Gateway 归属。`owner_id`、generation、lease 和 fencing 的原职责 MUST 保留，但不得被用于生成另一工作区 ID。

原有完整组排空、30 秒超时回退、显式 force、逐成员 interrupted 持久化、辅助服务保留和进程句柄只处理一次的合同 MUST 保留。恢复必须校验每个 UUID 对应的唯一 root binding、实际 mount 身份及完整成员集合；缺失或不符时 MUST 明确失败，不得部分 adopt、扫描磁盘或根据地址猜测。

#### Scenario: 共享 Backend 的 sibling 阻塞
- **WHEN** 工作区 A 发起重启，而同一 Backend 的工作区 B 仍有活动执行或代理流
- **THEN** 系统用 A、B 的真实 UUID 返回完整影响集合和 B 的 blockers；未经完整组确认不得强制中断，超时后旧 generation 与挂载集合保持可用

#### Scenario: Gateway 重启后恢复共享 Backend
- **WHEN** Gateway 从统一身份后的持久合同恢复共享 Backend
- **THEN** owner 只恢复一次，按各成员 UUID 解析唯一原生根绑定，完整 mount 验证成功后才发布全组在线，不要求旧 Gateway 工作区别名
