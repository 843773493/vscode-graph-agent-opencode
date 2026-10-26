## MODIFIED Requirements

### Requirement: 扩展工具和 MCP 必须使用统一调用入口

普通扩展工具和 MCP 工具 MUST 通过同一个 `invoke_extension_tool(tool_name, arguments)` 扩展工具调用入口执行，不得把 MCP 工具作为独立的模型直注册工具集合注入 Agent。扩展入口 MUST 在执行前使用目标完整公开 schema 校验参数，并把目标工具的真实错误返回给 Agent。完整契约 MUST 同时约束目录 revision、schema hash、发现正文及封存身份，不得只以 properties 摘要代表契约；合法参数不得被包装器静默删除或改变。

#### Scenario: 模型通过固定入口调用 MCP 工具

- **WHEN** Agent 需要执行一个已启用但不直接暴露 schema 的 MCP 工具
- **THEN** 模型 MUST 能调用固定扩展入口并提供目标工具名和参数对象，后端 MUST 路由到对应 MCP session 执行并返回结果

#### Scenario: MCP 工具参数不合法

- **WHEN** 固定扩展入口收到不符合 MCP 工具 schema 的参数
- **THEN** 后端 MUST 在真正发送 MCP 请求前返回参数校验错误，不得伪造成功结果或静默丢弃字段

#### Scenario: 只有顶层契约发生变化

- **WHEN** 工具 properties 不变，仅 required、additionalProperties、组合规则或引用定义发生变化
- **THEN** 完整契约身份反映该变化，旧调用不能被误判为与新 schema 等价

#### Scenario: schema 允许额外字段

- **WHEN** arguments 含 properties 外的字段，且完整 schema 允许这些字段
- **THEN** 校验接受合法字段并完整传给目标；schema 禁止时则在发送前返回配对的参数错误

### Requirement: 扩展工具的详细 schema 可选择暴露

扩展工具执行能力开启时，固定入口 MUST 始终保留最小的调用 schema；`model_visible=true` SHALL 控制目标名称、描述及完整参数 schema 的自动上下文注入。`model_visible=false` SHALL 只关闭自动注入，不禁止模型通过 Skill、普通明确上下文或显式 search_tools 获得定义后调用固定入口。显式发现 MUST 不改变 selection，不把完整目标定义注入 Provider 原生 tools，不绕过执行策略。

#### Scenario: 扩展工具默认隐藏详细定义

- **WHEN** 扩展工具首次启用且用户没有打开模型可见性，模型尚未显式请求定义
- **THEN** 模型请求中不得自动包含该扩展工具的完整参数 schema，但固定入口仍可用于真实调用

#### Scenario: 用户打开扩展工具模型可见性

- **WHEN** 用户打开扩展工具组的模型可见图标并到达下一允许激活边界
- **THEN** 模型请求上下文 MUST 包含该组目标工具的名称、描述和完整参数 schema

#### Scenario: 模型显式发现隐藏定义

- **WHEN** 模型通过 search_tools 请求允许执行但关闭自动注入的目标
- **THEN** 工具结果可提供完整定义，原 model_visible 值不变，不在后续请求自动注入全部目录

### Requirement: 扩展工具调用必须遵守统一有效策略

固定扩展入口 MUST 使用与工具目录相同的 ToolPolicyResolver 结果。策略关闭目标工具执行能力时，入口不得继续路由到该工具；策略仅隐藏模型定义时，入口仍可在 Skill、显式发现或其它明确上下文提供目标工具名后执行。执行 MUST 按产生调用的已封存目录及连接身份解析，再检查最新执行权限，不得由当前同名工具补位。

#### Scenario: 隐藏 schema 不影响扩展工具执行

- **WHEN** 扩展工具 execution_enabled=true 且 model_visible=false
- **THEN** 固定入口 MUST 保留该工具的真实参数校验和执行能力
- **AND** 未显式发现或通过其他上下文提供定义时，模型请求不得自动附带该工具的详细 schema

#### Scenario: 静态执行限制阻止固定入口

- **WHEN** 静态策略命中扩展工具的 restrictions.execution_disabled
- **THEN** 固定入口 MUST 在调用目标工具前返回明确的工具被禁用错误
- **AND** 不得触发目标扩展工具或 MCP session

#### Scenario: 目录封存后撤销权限

- **WHEN** 模型按旧允许策略发出工具调用，而执行前权限已撤销
- **THEN** 返回与原 tool_call_id 配对的真实拒绝，不使用已保存 schema 作为权限许可

## ADDED Requirements

### Requirement: 并发扩展调用保留逐调用行为

模型 SHALL 通过同一轮多次既有信封调用表达无依赖的多个操作；每个调用 MUST 保留独立调用 ID、冻结身份、人工确认、期限、取消和结果。依赖后续结果的操作 MUST 在后续步骤发起。本变更 MUST 不引入平行批量 dispatcher 或更改既有信封参数形状。

#### Scenario: 同轮一个调用失败另一个成功

- **WHEN** 两个独立信封调用分别遇到参数错误和远端成功结果
- **THEN** 各自得到与原调用 ID 配对的真实结果，一个错误不伪造另一调用失败或整批成功
