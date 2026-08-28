## Purpose

统一普通扩展工具与 MCP 工具的发现、分组、模型提示和真实调用边界，使扩展工具可以隐藏详细 schema 但仍可通过 Skill 或其它上下文可靠调用。

## ADDED Requirements

### Requirement: 扩展工具和 MCP 必须使用统一调用入口

普通扩展工具和 MCP 工具 MUST 通过同一个扩展工具调用入口执行，不得把 MCP 工具作为独立的模型直注册工具集合注入 Agent。扩展入口 MUST 在执行前使用目标工具的真实 schema 校验参数，并把目标工具的真实错误返回给 Agent。

#### Scenario: 模型通过固定入口调用 MCP 工具

- **WHEN** Agent 需要执行一个已启用但不直接暴露 schema 的 MCP 工具
- **THEN** 模型 MUST 能调用固定扩展入口并提供目标工具名和参数对象，后端 MUST 路由到对应 MCP session 执行并返回结果

#### Scenario: MCP 工具参数不合法

- **WHEN** 固定扩展入口收到不符合 MCP 工具 schema 的参数
- **THEN** 后端 MUST 在真正发送 MCP 请求前返回参数校验错误，不得伪造成功结果或静默丢弃字段

### Requirement: MCP 工具目录必须归入扩展工具组

MCP 工具 MUST 作为 `kind="extension"` 出现在工具目录中；同一 MCP Server 的工具 MUST 归入 `group_id="mcp:{server_id}"`，组名 MUST 使用 `扩展工具 · MCP · {server_id}`，不得继续使用独立 MCP 顶层分类。

#### Scenario: MCP Server 目录展示

- **WHEN** 一个已连接 MCP Server 返回多个远程工具
- **THEN** 工具目录 MUST 为这些工具返回统一扩展类型、相同 MCP 组 ID 和 `扩展工具 · MCP · {server_id}` 组名

#### Scenario: MCP 工具命名冲突

- **WHEN** 两个远程工具归一化后产生相同工具 ID
- **THEN** MCP runtime MUST 明确报错并拒绝启动或刷新，不得静默覆盖任一工具

### Requirement: 扩展工具的详细 schema 可选择暴露

扩展工具执行能力开启时，固定入口 MUST 始终保留最小的调用 schema；只有 `model_visible=true` 时，模型请求上下文才可以附带该目标扩展工具的名称、描述和参数 schema。`model_visible=false` 时，模型仍可通过 Skill 或其它明确上下文获知目标工具名并调用固定入口。

#### Scenario: 扩展工具默认隐藏详细定义

- **WHEN** 扩展工具首次启用且用户没有打开模型可见性
- **THEN** 模型请求中不得包含该扩展工具的完整参数 schema，但固定入口仍可用于真实调用

#### Scenario: 用户打开扩展工具模型可见性

- **WHEN** 用户打开扩展工具组的模型可见图标并发送下一次请求
- **THEN** 模型请求上下文 MUST 包含该组目标工具的名称、描述和参数 schema

### Requirement: Gateway 必须透明转发工具能力协议

Workspace Gateway MUST 对工具目录和工具状态更新 API 透明代理完整的双状态字段、MCP 扩展分组字段和错误响应，并在本地工作区、远程 Gateway 工作区两种路由下保持同一请求头、认证和工作区目标选择语义。

#### Scenario: 浏览器通过本地 Gateway 更新工具状态

- **WHEN** 浏览器通过 `/api/v1/tools` 和 `/api/v1/tools/selection` 访问当前工作区
- **THEN** Gateway MUST 把请求转发至当前激活工作区后端并返回完整最新工具状态

#### Scenario: 联邦 Gateway 转发 MCP 工具目录

- **WHEN** 当前 Gateway 代理一个由远程 Gateway 管理的工作区工具目录
- **THEN** 返回结果 MUST 保留 `扩展工具 · MCP · {server_id}` 分组和双状态字段，且不得把本地 Gateway 的工具状态混入远程工作区

### Requirement: 扩展工具调用必须遵守统一有效策略

固定扩展入口 MUST 使用与工具目录相同的 `ToolPolicyResolver` 结果。策略关闭目标工具执行能力时，入口不得继续路由到该工具；策略仅隐藏模型定义时，入口仍可在 Skill 或其它明确上下文提供目标工具名后执行。

#### Scenario: 隐藏 schema 不影响扩展工具执行

- **WHEN** 扩展工具 `execution_enabled=true` 且 `model_visible=false`
- **THEN** 固定入口 MUST 保留该工具的真实参数校验和执行能力
- **AND** 模型请求不得附带该工具的详细 schema

#### Scenario: 静态执行限制阻止固定入口

- **WHEN** 静态策略命中扩展工具的 `restrictions.execution_disabled`
- **THEN** 固定入口 MUST 在调用目标工具前返回明确的工具被禁用错误
- **AND** 不得触发目标扩展工具或 MCP session
