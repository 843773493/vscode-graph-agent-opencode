## Context

当前 `NodeDebugService` 已经按 session 管理 Node Inspector 进程、WebSocket 命令、断点、调用栈、变量 hydration、求值和动作记录；Node 调试 HTTP API 也已经存在。Agent runtime 当前通过 `build_default_tools` 构建直接工具，并通过 `ConfigService` 解析工具策略和 `confirmation_required`。现有默认工具工厂没有调试服务依赖，Agent runtime dependency provider 也没有暴露 `NodeDebugService`。

本变更需要把已有 Node 调试能力映射为 Agent 工具，同时保留未来 adapter 的边界。当前不引入 VS Code 扩展或 debugpy 依赖，也不把调试端口、线程和 frame 标识交给模型。

## Goals / Non-Goals

**Goals:**

- 通过直接的 LangChain tools 暴露 DebugMCP 风格的 16 个工具。
- 将工具调用的 session、workspace、tool call identity 和配置依赖由闭包/运行时容器注入。
- 为 Node Inspector 实现生命周期、执行控制、普通/条件断点、变量检查和表达式求值。
- 对 Node 当前不具备的 logpoint 能力返回明确的不支持结果，避免伪装成普通断点。
- 将 `runtime.debug` 接入现有 Workspace JSONC 合并和 schema 校验。
- 使用统一 JSON 文本结果，兼容现有 Agent tool output middleware，同时保持完整调试快照。
- 通过现有工具策略支持调试工具禁用和表达式求值人工确认。
- 用纯后端 E2E 验证工具目录、真实 Node fixture、session 隔离、断点控制、变量/表达式和配置覆盖。

**Non-Goals:**

- 本次不实现 Python debugpy adapter、通用 DAP adapter 或 VS Code Debug API 路由。
- 本次不支持 Agent 选择任意 thread/frame，不支持修改运行时变量。
- 本次不允许 Agent 通过 Inspector/debugpy 端口连接外部进程。
- 本次不改变 Web 调试面板的整体布局；面板只消费已有或新增的后端快照。
- 本次不把 `runtime.debug` 的 `program`、`runtime` 或 `adapter` 直接加入 Agent 工具输入。

## Decisions

### 1. Use direct tools and the existing tool policy

在 `app/agents/tools/debugging.py` 中提供 16 个 `StructuredTool` factory，并从 `build_default_tools` 直接加入工具列表。工具名称加入默认工具全集和调试工具目录分组，因而 Agent 可以直接调用原始工具名，而不是先调用 `invoke_custom_tool` 再二次分发。

工具是否可调用仍由现有 `denylist`、`allowlist` 和 `confirmation_required` 解析；`evaluate_expression` 不做特殊的绕过路径。这样既满足 DebugMCP schema 兼容性，也沿用当前工具策略的一致行为。

替代方案是把 16 个工具作为 `tools.custom` 扩展，仅暴露 `invoke_custom_tool`。该方案更符合现有扩展工具机制，但会改变 Agent 看到的协议形态、增加文档依赖，并且无法满足本变更的直接 MCP 兼容目标，因此不采用。

### 2. Add a narrow Agent-facing facade over NodeDebugService

工具 factory 不直接拼装 Inspector 命令，而是调用一个面向 Agent 的调试 facade。该 facade 负责：

- 把 DebugMCP 的 `fileFullPath` / `workingDirectory` 转换为当前 workspace 内的安全相对路径；
- 通过 `configurationName` 解析 launch profile；
- 把工具动作映射为现有 Node 调试 action；
- 对 `list_variable_names` 和 `get_variables_values` 生成稳定的 scope 结果；
- 统一包装成功/失败 JSON；
- 使用 `ToolInvocationContext.require_tool_call_id()` 写入动作审计。

首期 facade 只实现 Node Inspector adapter。adapter 选择必须来自已解析的配置 profile；除 `node_inspector` 外的 adapter 返回明确不支持错误。

### 3. Keep runtime identifiers internal

`NodeDebugService` 继续按 session 保存运行时。Agent tool factory 闭包保存当前 session ID 和 NodeDebugService 引用，所有调用都使用该 session。Node Inspector 的实际端口和 WebSocket URL 只在基础设施内部使用；状态对 Agent 返回端口脱敏后的调试快照，不把可用于跨目标连接的完整地址作为输入或控制句柄。

Node 当前只使用顶层 call frame 做变量和求值。`call_frame_id` 继续作为服务内部字段；不把它配置化，也不新增 Agent-facing `frameId` / `threadId`。

### 4. Resolve configuration through ConfigService

扩展 `ConfigService`，提供规范化的 debug runtime 配置读取方法。配置 schema 在 `runtimeConfig` 下增加 `debug`、`node`、`python` 和 `launch_profiles` 定义；默认配置在 `workspace_inline.jsonc` 中提供 loopback、动态端口和 Node Inspector 默认 profile。

NodeDebugService 读取当前 pinned/effective config，而不是自行解析 JSONC。配置合并继续使用已有的 inline → user → user_local → workspace 覆盖顺序。新字段全部可选，不升级 `config_version`；schema 仍禁止未知字段。

`inspector_port: 0` 通过 Node 的动态监听方式实现。固定端口仅作为显式本地开发配置支持，且 host 必须是 loopback；8211 不写入 debug 默认值。

### 5. Extend the existing Node debug state minimally

沿用现有 `NodeDebugStateDTO`、断点、调用栈、求值和动作模型，补充工具所需的条件/logpoint 元数据和工具调用审计字段。普通/条件断点使用 Inspector `Debugger.setBreakpointByUrl` 的 condition 能力。

Node Inspector 没有与 VS Code `SourceBreakpoint.logMessage` 等价的通用 logpoint API。首期 `add_logpoint` 必须先验证 adapter capability；Node adapter 对它返回明确 `UNSUPPORTED_DEBUG_FEATURE`，不安装会暂停执行的普通断点。未来 adapter 可以在不改变 Agent schema 的情况下实现真正的 logpoint。

### 6. Return JSON text for LangChain compatibility

当前 Agent 工具和 ToolOutputMiddleware 已经稳定处理字符串结果，因此工具返回 `json.dumps` 生成的 JSON 文本：成功为 `{"ok":true,"message":...,"state":...}`，失败为 `{"ok":false,"error":{"code":...,"message":...},"state":...}` 或抛出带明确错误的工具异常。HTTP API 继续返回现有 `APIResponse`，不把 Agent 的 JSON 文本格式反向强加给 Web API。

### 7. Test the real backend without browser dependencies

新增 `tests/e2e/backend/agents/test_debug_tools.py`，使用 E2E fixture workspace 写入一个稳定的 JS 调试 fixture，通过 Agent tool factory 直接 `ainvoke` 工具，并检查真实 Node 进程状态、断点停靠、变量、表达式、单步、清理和 session 隔离。

测试还通过临时 workspace `.boxteam/workspace.jsonc` 验证 `runtime.debug` profile 覆盖和配置 schema；不启动 Web、Gateway 或 VS Code，不运行 `reference_repo` 测试。

## Risks / Trade-offs

- [Node Inspector 不支持通用 logpoint] → 首期返回明确不支持错误；测试确认不会错误安装普通暂停断点。
- [表达式求值可以执行副作用代码] → 复用工具确认策略、强制记录 tool call identity，并保持 Inspector loopback/session 绑定。
- [直接加入默认工具会增加 Agent tool catalog] → 工具目录使用独立 `debugging` 分组；工具策略仍可整体或逐项 denylist。
- [配置 profile 可能与当前 Node 直接启动参数冲突] → 先定义规范化 profile 解析，未配置时完全回退到现有 Node 默认行为；固定端口和外部 adapter 不默认启用。
- [运行时状态与异步 Inspector 事件存在竞态] → 所有工具动作等待后端 authoritative snapshot；变量 hydration 完成后才返回暂停快照；E2E 保持暂停状态并验证重复读取。
- [错误结果需要同时满足 Agent 和现有工具错误处理] → 使用稳定错误 code 的 JSON 文本并保留异常边界；不返回虚假默认状态。

## Migration Plan

1. 更新 Workspace schema 和 inline config，新增可选的 `runtime.debug` 默认配置，不改旧用户配置文件。
2. 更新 Agent 工具全集、目录分组和运行时依赖注入；默认 Node profile 使用现有动态 Inspector 行为。
3. 部署后，未配置 `runtime.debug` 的工作区继续使用 Node 默认值；没有 Node 可执行文件时工具返回明确错误。
4. 如需回滚，移除调试工具配置/denylist 入口并停止 Agent 创建的 Node runtime；已有 HTTP Node 调试 API 保持可用。
5. 未来增加 debugpy 或 VS Code adapter 时，只新增 adapter 实现和 profile 校验，不改变 16 个 Agent 工具的输入 schema。
