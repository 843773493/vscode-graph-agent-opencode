## Purpose

定义经已认证 SSH 会话建立的 Gateway 联邦，包括单一控制端口隧道、远程工作区投影和代理、后端生命周期委托、独立凭据认证、协议兼容检查，以及禁止循环和嵌套导出的有界拓扑。

## Requirements

### Requirement: SSH 连接远程 Gateway
SSH 远程连接 SHALL（必须）只转发远程 Gateway 控制端点，MUST NOT（不得）直接转发远程 Workspace API、Terminal Manager 或 Browser Manager 端点。

#### Scenario: 建立远程连接
- **WHEN** 用户通过 SSH 连接远程 BoxTeam 安装
- **THEN** 本地 Gateway 在导入远程 Gateway 直接管理的工作区之前验证其协议

### Requirement: 联邦工作区投影
本地 Gateway SHALL（必须）使用远程 Gateway 连接标识和远程工作区标识表示远程工作区，并 SHALL（必须）通过远程 Gateway 路由业务请求。

#### Scenario: 请求远程工作区
- **WHEN** 当前活动工作区属于远程 Gateway
- **THEN** 本地 Gateway 携带远程工作区标识向该 Gateway 转发请求，并保留原 request ID

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
