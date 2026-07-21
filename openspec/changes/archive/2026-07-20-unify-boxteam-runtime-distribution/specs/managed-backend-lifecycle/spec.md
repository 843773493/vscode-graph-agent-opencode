## ADDED Requirements

### Requirement: Gateway 拥有所有本地托管工作区
Gateway SHALL（必须）拥有默认本地工作区 runtime 和每个新增的本地托管工作区 runtime；显式声明为外部管理的本地后端 SHALL（必须）保持仅可探测。

#### Scenario: 默认工作区启动
- **WHEN** Gateway 使用有效默认工作区启动
- **THEN** Gateway 启动该工作区、将其注册为托管工作区，并提供后端重启操作

#### Scenario: 外部本地后端
- **WHEN** 工作区指向由用户管理的本地后端
- **THEN** Gateway 提供健康探测，但拒绝重启其进程

### Requirement: 按服务管理生命周期
Gateway MUST（必须）使用具名 service handle 跟踪 Workspace API、Terminal Manager 和 Browser Manager；因后端配置执行重启时 SHALL（必须）只重启 Workspace API。

#### Scenario: 仅重启后端
- **WHEN** 安全重启托管工作区后端
- **THEN** 该工作区的 Terminal、Browser 进程及服务 URL 保持不变

#### Scenario: 移除工作区
- **WHEN** 删除可移除的托管工作区
- **THEN** Gateway 优雅关闭该工作区的全部具名服务

### Requirement: 安全重启排空
Workspace API 在 draining 期间 SHALL（必须）停止接受新 Job，并 SHALL（必须）报告真实 blockers。Gateway SHALL（必须）最多等待 30 秒；若排空超时且未显式 force，SHALL（必须）保持旧后端运行。

#### Scenario: 没有活动任务
- **WHEN** 请求安全重启且后端报告没有 blockers
- **THEN** Gateway 优雅停止并重启 Workspace API，在报告成功前验证其健康状态

#### Scenario: 活动任务超过超时
- **WHEN** blockers 持续 30 秒且没有请求 force
- **THEN** Gateway 报告 blockers，并保持现有后端运行

### Requirement: 显式强制重启
Gateway MUST（必须）在中断活动任务前要求显式 force 请求；Workspace API MUST（必须）在进程终止前把受影响任务持久化为 interrupted。

#### Scenario: 用户确认强制重启
- **WHEN** 展示 blockers 后用户请求 force 重启
- **THEN** Gateway 终止后端前，将活动 Job 标记为 interrupted 并记录重启原因

### Requirement: 重启状态对账
Workspace API MUST（必须）在启动时对账已持久化的执行状态，确保没有缺少活动执行所有者的记录仍保持 running。

#### Scenario: Job 执行期间后端退出
- **WHEN** 启动时发现来自上一 runtime generation 的已持久化 running Job
- **THEN** 系统记录可观察的 interrupted 状态，而不是继续把 Job 显示为 running

### Requirement: 托管进程组清理
Gateway SHALL（必须）管理服务进程组，并 SHALL（必须）在优雅关闭超时后终止完整进程组。

#### Scenario: 子工具忽略关闭请求
- **WHEN** 托管子进程在优雅关闭期限后仍存活
- **THEN** Gateway 终止服务进程组，并报告所有清理失败
