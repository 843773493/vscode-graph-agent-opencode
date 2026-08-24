## Purpose

为工具目录提供可理解且可独立控制的能力状态，让用户能够分别决定 Agent 是否可以执行工具，以及模型是否收到工具说明和参数 schema。

## ADDED Requirements

### Requirement: 工具必须暴露两个独立能力状态

工作区后端工具目录 MUST 为每个工具返回 `execution_enabled` 和 `model_visible` 两个布尔状态。`execution_enabled` 表示工具是否注册到当前 Agent runtime 并允许实际执行；`model_visible` 表示工具的名称、描述和模型参数 schema 是否可以出现在模型请求的工具定义中。

#### Scenario: 默认工具同时可执行且对模型可见

- **WHEN** 用户首次查看默认工具组中的一个工具，且此前没有保存该工具的覆盖设置
- **THEN** 工具目录返回 `execution_enabled=true` 和 `model_visible=true`

#### Scenario: 扩展工具默认可执行但不暴露详细定义

- **WHEN** 用户首次查看普通扩展工具或 MCP 扩展工具，且此前没有保存该工具的覆盖设置
- **THEN** 工具目录返回 `execution_enabled=true` 和 `model_visible=false`

#### Scenario: Source Debugging 默认不向模型展示

- **WHEN** Agent 首次加载包含 `kind="debugging"` 的 Source Debugging 工具组，且用户没有保存模型可见性覆盖
- **THEN** 这些工具 MUST 返回 `execution_enabled=true` 和 `model_visible=false`
- **AND** 用户打开模型可见性后，后续请求 MUST 可以把这些工具定义提供给模型

#### Scenario: 关闭执行能力自动撤销模型可见性

- **WHEN** 用户把一个工具的 `execution_enabled` 设置为 `false`
- **THEN** 后端 MUST 同时把该工具的 `model_visible` 保存为 `false`，并在后续目录响应中返回两个状态均为 `false`

### Requirement: 工具状态更新必须原子且按 Agent 持久化

工具状态更新 MUST 按 Agent 和工作区持久化；一次请求中包含的多个工具变更 MUST 全部成功后再提交。未知工具、重复工具或违反能力联动约束的请求 MUST 被拒绝，且不得写入部分结果。

#### Scenario: 组级更新一次提交全部工具

- **WHEN** 用户把一个工具组的执行能力或模型可见性切换为目标状态
- **THEN** 后端 MUST 对该组当前目录中的全部工具执行同一事务更新，并返回这些工具的完整最新状态

#### Scenario: 非法变更保持原状态

- **WHEN** 请求包含不存在的工具 ID、重复工具 ID，或把未执行的工具设置为模型可见
- **THEN** API MUST 返回明确的客户端错误，且该请求涉及的所有工具状态保持不变

### Requirement: 工具目录状态必须支持混合状态

前端和 API MUST 能表达一个工具组内部分工具开启、部分工具关闭，以及部分工具对模型可见的混合状态；组级图标 MUST 不使用复选框伪造混合状态。

#### Scenario: 组内状态部分开启

- **WHEN** 一个组内只有部分工具的执行能力为 `true`
- **THEN** 工具组响应 MUST 保留每个工具的真实状态，前端组级执行图标显示混合状态并允许一次性设置整个组

#### Scenario: 模型可见性只允许在执行能力开启时生效

- **WHEN** 一个组内存在执行能力关闭的工具
- **THEN** 前端 MUST 禁用这些工具对应的模型可见图标，后端 MUST 拒绝或归一化任何令其可见的状态

### Requirement: 运行时必须区分执行注册和模型工具定义

Agent runtime MUST 将 `execution_enabled=true` 的工具保留在执行注册表中；模型请求 MUST 只接收 `model_visible=true` 且执行能力开启的直接工具定义。用户关闭模型可见性不得导致已启用工具的后端校验和执行注册丢失。

#### Scenario: 工具执行开启但定义隐藏

- **WHEN** 工具的执行能力为 `true` 且模型可见性为 `false`
- **THEN** Agent runtime 可以执行该工具，但模型请求的工具定义列表不得包含该工具的名称、描述或参数 schema

#### Scenario: 刷新或新建 Agent runtime 使用最新状态

- **WHEN** 用户更新工具状态后开始下一次 Agent 请求
- **THEN** 下一次请求 MUST 使用最新持久化状态，不得继续使用更新前的工具可见性或执行集合

#### Scenario: MCP JSON Schema 仍然执行公开参数校验

- **WHEN** 固定扩展入口收到一个 MCP 工具调用，且该 MCP 工具提供 JSON Schema 字典而不是 Pydantic `tool_call_schema`
- **THEN** 后端 MUST 按该公开 JSON Schema 校验参数；合法参数才可以进入 MCP `ainvoke`，非法参数 MUST 在工具执行前明确失败。

#### Scenario: 配置并发更新不互相覆盖

- **WHEN** 两个工作区 Agent 同时更新同一个 Agent 下不同工具的能力状态
- **THEN** 选择存储 MUST 使用跨进程锁保护完整读改写事务，最终配置 MUST 同时保留两个请求的有效变更。
