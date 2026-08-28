## Why

当前工具面板只有一个 `enabled` 勾选状态，无法区分“工具是否可被运行时调用”和“工具定义是否发送给模型”。同时 MCP 工具仍直接注册为模型工具，绕过了扩展工具统一入口，导致工具目录、模型可见性和实际执行能力的语义不一致。

## What Changes

- **BREAKING** 将工具选择状态拆为“执行可用”和“模型可见定义”两个独立能力；关闭执行能力时强制关闭模型可见性。
- **BREAKING** 将工具选择 API、持久化状态和 Agent runtime 装配改为使用双状态，不再使用单一 `enabled` 作为完整语义。
- 将 Web 工具面板的勾选控件改为两个带悬停说明的图标按钮：工具能力按钮、模型可见性按钮；组级和工具级操作保持一致。
- 默认工具组同时启用两项能力；扩展工具组默认允许执行但不向模型暴露工具定义。
- 将工具默认策略从代码硬编码移入 Workspace JSONC，按来源、类型、工具组和具体工具提供可扩展规则，并区分可被用户覆盖的默认值与不可覆盖的硬限制。
- 统一工具目录元数据中的 `origin`、`kind`、`group_id`，让配置、后端目录、Agent runtime 和 Web 使用同一套匹配语义。
- JSONC 只负责 Workspace/Agent 的静态策略；本次不实现跨电脑同步，前端运行时覆盖继续属于当前工作区状态。
- 将 MCP 工具纳入扩展工具组，统一通过 `invoke_custom_tool` 调用；工具组名称统一为“扩展工具 · MCP · {具体名称}”。
- MCP 工具的真实参数 schema 仍由后端保留并用于执行校验；只有模型可见性开启时才发送给模型上下文。
- Gateway 继续作为工作区工具 API 的透明代理，但补充双状态 API 的代理、远程 Gateway 路由和请求头测试，确保工具状态在实际工作区后端生效。
- 删除旧的 MCP 直注册目录语义和前端复选框交互，不保留兼容字段或旧 UI 分支。
- 不在本次变更中实现跨电脑同步、用户级工具选择同步或 Gateway 全局工具状态。

## Capabilities

### New Capabilities

- `tool-capability-controls`: 定义执行可用性、模型可见性、默认值、联动约束、持久化和 API 契约。
- `extension-tool-invocation`: 定义普通扩展工具与 MCP 工具通过统一扩展入口注册、展示、schema 暴露和实际执行的规则。

### Modified Capabilities

无。

## Impact

- 工作区后端工具状态 API、工具目录服务、选择状态存储和 Agent runtime 装配。
- MCP runtime manager、工具命名与 MCP 目录 DTO。
- Workspace Gateway 的工作区 API 透明代理测试和远程 Gateway 转发验证。
- `src/clients/web` 工具面板、工具组/工具项交互、无障碍状态和 CSS。
- 后端单元/集成测试、Gateway 路由测试、Web 组件测试和 OpenAPI/生成类型。
