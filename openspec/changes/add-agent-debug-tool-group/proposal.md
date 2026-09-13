## Why

当前工作区已经具备 Node Inspector 源码调试服务和 Web 调试面板，但 Agent 只能通过间接的 Job/API 控制动作使用它，无法像 DebugMCP 一样直接调用一组稳定的调试工具。需要定义兼容 DebugMCP 的 Agent 工具契约，并将调试后端、启动配置和端口策略纳入工作区配置，以便 Agent 可以在当前会话内自主设置断点、暂停、单步和检查运行时状态。

## What Changes

- 新增一组直接暴露给 Agent 的源码调试工具，工具名称和输入字段尽量兼容 DebugMCP。
- 覆盖调试会话生命周期、继续/暂停/单步、断点与 Logpoint、变量查看和表达式求值。
- 首期使用现有 Node Inspector 实现 JavaScript 调试；为未来 debugpy、DAP 和 VS Code 调试适配预留后端边界，但本次不实现 VS Code 会话路由。
- 新增 `runtime.debug` 工作区配置，支持默认 adapter、Node Inspector、debugpy 预留配置和可命名的 launch profile。
- 默认使用 loopback 和动态调试端口，禁止将 Web 端口 8211 作为 Inspector 或 debugpy 端口。
- 将当前 Agent session 作为调试资源边界；不向 Agent 暴露 `vscodeSessionId`、`threadId`、`frameId`、Inspector WebSocket 地址等运行时内部标识。
- 对表达式求值和其他调试动作记录可审计的调试动作；表达式求值支持沿用 Agent 工具确认策略进行配置。
- 返回统一的调试状态和错误结构，同时保留足够信息供 Web 调试面板和 Agent 继续工作。

## Capabilities

### New Capabilities

- `agent-debug-tool-group`: 定义 Agent 可直接调用的 16 个源码调试工具、输入 schema、生命周期语义、状态返回和错误行为。
- `debug-runtime-configuration`: 定义 `runtime.debug` 工作区配置、Node Inspector 默认值、未来 adapter/launch profile 扩展点、端口和安全边界。

### Modified Capabilities

- 无。现有配置初始化和运行时加载能力继续负责加载、合并和校验新增的可选配置字段，不改变其生命周期契约。

## Impact

- Agent 工具注册和工具策略：`app/agents/agent_tools.py`、`app/agents/tools/`、内置工具注册与 Agent 配置选择。
- 调试业务与基础设施：现有 `NodeDebugService`、Agent session 上下文、调试动作审计和 Node 调试 API。
- 配置文件与 schema：`configs/workspace_inline.jsonc`、`configs/workspace_schema.jsonc`，以及工作区 `.boxteam/workspace.jsonc` 覆盖。
- Web 调试面板和 SSE 状态消费可能需要适配统一的工具动作/状态模型。
- 不新增外部调试服务依赖；debugpy、VS Code Debug API、DAP 连接器仅作为后续扩展点。
