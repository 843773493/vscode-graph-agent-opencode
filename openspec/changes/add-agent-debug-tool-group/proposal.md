## Why

当前工作区已经具备 Node Inspector 源码调试服务和 Web 调试面板，但 Agent 只能通过间接的 Job/API 控制动作使用它，无法像 DebugMCP 一样直接调用一组稳定的调试工具。需要定义兼容 DebugMCP 的 Agent 工具契约，并将调试后端、启动配置和端口策略纳入工作区配置，以便 Agent 可以在当前会话内自主设置断点、暂停、单步和检查运行时状态。

## What Changes

- 新增一组通过固定 `invoke_custom_tool` 扩展入口提供给 Agent 的源码调试工具；16 个执行工具尽量兼容 DebugMCP，另有 4 个会话方案管理工具，20 个目标工具均不直接注册到 Agent runtime 的模型工具列表。
- 覆盖调试会话生命周期、继续/暂停/单步、断点与 Logpoint、变量查看和表达式求值。
- 首期使用现有 Node Inspector 实现 JavaScript 调试；为未来 debugpy、DAP 和 VS Code 调试适配预留后端边界，但本次不实现 VS Code 会话路由。
- 新增 `runtime.debug` 工作区配置，支持默认 adapter、Node Inspector、debugpy 预留配置和可命名的 launch profile。
- 默认使用 loopback 和动态调试端口，禁止将 Web 端口 8211 作为 Inspector 或 debugpy 端口。
- 将当前 Agent session 作为调试资源边界；不向 Agent 暴露 `vscodeSessionId`、`threadId`、`frameId`、Inspector WebSocket 地址等运行时内部标识。
- 对表达式求值和其他调试动作记录可审计的调试动作；表达式求值支持沿用 Agent 工具确认策略进行配置。
- 返回统一的调试状态和错误结构，同时保留足够信息供 Web 调试面板和 Agent 继续工作。
- 新增产品级 `debugging` Skill，说明 20 个工具的 JSON schema、方案选择、断点到结束的调用顺序和安全边界；E2E 工作区从 `resources/skills/` 同步 Skill，不在 asset 中复制产品源码。
- 新增提示词驱动的纯后端 E2E，经过真实 HTTP session、模型 tool-call 协议、Agent runtime 和真实 Node Inspector；另提供显式开关的真实外部模型验证。
- 将人类和 Agent 视为同一会话源码调试运行时的并列操作者，删除“接管/交接/控制模式”语义；任一方动作后都以最新权威状态继续。
- 将最近一次启动参数、选中 profile 和源码断点持久化到当前会话节点，使人类启动后可以直接通过会话消息让 Agent 接着调试。
- 为断点保存源码锚点并在源码变化后执行保守失效；不自动重定位，相关断点标记为 `pending_update`，活动进程使用旧源码时标记需要重启但不阻断控制动作，不静默绑定到错误行。
- 优化 debugging Skill 和调试工具描述，使模型按权威 `state.status` 选择最少动作，理解人类并发操作，并在开始/结束边界正确处理 `invalid_breakpoints`，避免机械重复查询、误报断点命中或误把失效断点当成阻断错误。
- 使用跨两个 JavaScript 模块的 fixture 验证断点、调用栈和 Web 源码预览随实际暂停文件自动切换。
- 删除工具调用前后、模型请求前等 Agent 自身执行循环断点及其 Job 控制、事件协议和 Web 界面；源码调试只操作用户显式选择的目标程序。
- 一个会话可以保存、切换和删除多套源码调试方案；运行时仍只激活其中一套，避免同时运行的目标状态互相覆盖。
- 每套方案以不含 session ID、PID、Inspector 地址和动作历史的独立版本化 JSON 文件保存。文件使用工作区相对路径，可直接复制到另一会话的方案目录，也可通过 API 完成跨会话复制。
- 将源码断点扩展为普通断点、条件断点、命中次数断点和日志点；用户可在源码行号槽左键快速切换普通断点，也可右键创建、编辑或删除特殊断点。
- Node Inspector adapter 通过不会暂停目标程序的条件表达式实现日志点，并在调试输出中展示日志；命中次数断点按本次目标进程内的命中次数触发。

## Capabilities

### New Capabilities

- `agent-debug-tool-group`: 定义 Agent 通过固定扩展入口调用的 16 个源码执行工具和 4 个会话方案工具、输入 schema、生命周期语义、状态返回和错误行为。
- `debug-runtime-configuration`: 定义 `runtime.debug` 工作区配置、Node Inspector 默认值、未来 adapter/launch profile 扩展点、端口和安全边界。

### Modified Capabilities

- 无。现有配置初始化和运行时加载能力继续负责加载、合并和校验新增的可选配置字段，不改变其生命周期契约。

## Impact

- Agent 工具注册和工具策略：`app/agents/agent_tools.py`、`app/agents/tools/`、内置工具注册与 Agent 配置选择。
- 调试业务与基础设施：现有 `NodeDebugService`、Agent session 上下文、调试动作审计和 Node 调试 API。
- 配置文件与 schema：`configs/workspace_inline.jsonc`、`configs/workspace_schema.jsonc`，以及工作区 `.boxteam/workspace.jsonc` 覆盖。
- Web 调试面板和 SSE 状态消费可能需要适配统一的工具动作/状态模型。
- Web 右侧侧边栏源码预览与扩展窗口源码区共享特殊断点菜单和权威断点状态。
- 会话节点新增源码调试配置记录；Web 右侧侧边栏与扩展窗口继续消费同一份后端状态。
- Agent 自身执行调试的 schema、服务、事件、Job API、前端入口和测试全部删除，不保留兼容字段或无效路由。
- Skill 资源打包和 E2E 工作区准备器：`resources/skills/debugging/`、`tests/support/workspaces.py`、`tests/e2e/backend/agents/test_debug_prompt_flow.py`。
- 不新增外部调试服务依赖；debugpy、VS Code Debug API、DAP 连接器仅作为后续扩展点。
