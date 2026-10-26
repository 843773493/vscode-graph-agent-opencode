## MODIFIED Requirements

### Requirement: Workspace 配置按生效范围分类

Workspace SHALL（必须）为有效配置路径声明可观察的生效范围。模型、Agent 和开发运行参数的变化 MUST（必须）至少对后续 Job 生效；默认 Agent 的变化 MUST（必须）只影响后续新会话；UI 和按请求读取的配置变化 MUST（必须）对后续读取生效；当前 Job、当前会话的既有绑定和已经建立的请求 MUST（必须）保持原语义。已完成安全替换验收的 MCP Server 连接字段 SHALL（必须）按工作区受控热应用，通过目录激活边界影响后续模型调用，不重建正在运行的 Agent 或改投已封存调用；未覆盖的依赖 MUST（必须）继续明确分类为需重启，不得将未知字段一律当作可热应用。

#### Scenario: 修改模型或 Agent 配置

- **WHEN** 有效 llm 或 agents 配置热重载成功
- **THEN** 后续 Job 使用新配置，正在运行的 Job 不被替换或重建

#### Scenario: 修改默认 Agent

- **WHEN** 有效 default_agent 发生变化
- **THEN** 新建会话使用新的默认 Agent，已有会话继续使用其已持久化的 Agent 标识

#### Scenario: 修改已被启动时缓存的运行时依赖

- **WHEN** 修改按当前有效策略仍不能安全替换的 Workspace 运行时依赖
- **THEN** 系统报告 Workspace backend restart required，并不得声称该配置已经即时生效

#### Scenario: 修改受支持的 MCP 连接配置

- **WHEN** 有效 MCP Server command、args、env、cwd、URL、headers、auth 引用、enabled 或增删变更通过相应监督替换验收和候选准备
- **THEN** 在当前工作区受控切换、确认 active 后对后续允许边界生效，其他工作区和旧封存调用保持各自身份
