## MODIFIED Requirements

### Requirement: Gateway 拥有所有本地托管工作区

Gateway SHALL（必须）拥有默认本地工作区 runtime 和每个新增的本地托管工作区 runtime；显式声明为外部管理的本地后端 SHALL（必须）保持仅可探测。runtime 所有权 MUST 以「工作区身份」为粒度，而非以「一个工作区一个后端进程」为隐含前提：一个托管后端进程 MAY 挂载多个工作区，每个工作区的重启、排空与进程组清理 MUST 以本 change 的**已挂载工作区注册表**所登记的 `workspace_id` 集合为准。

#### Scenario: 默认工作区启动

- **WHEN** Gateway 使用有效默认工作区启动
- **THEN** Gateway 启动该工作区、将其注册为托管工作区，并提供后端重启操作

#### Scenario: 外部本地后端

- **WHEN** 工作区指向由用户管理的本地后端
- **THEN** Gateway 提供健康探测，但拒绝重启其进程

#### Scenario: 一个托管后端挂载多个工作区

- **WHEN** 一个托管后端进程按本 change 的注册表挂载多个工作区
- **THEN** Gateway MUST 以工作区身份而非进程身份跟踪所有权，且 MUST NOT 假定每个工作区各有一个独立后端进程

