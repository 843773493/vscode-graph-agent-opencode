## Context

当前 `NodeDebugService` 已经按 session 管理 Node Inspector 进程、WebSocket 命令、断点、调用栈、变量 hydration、求值和动作记录；Node 调试 HTTP API 也已经存在。Agent runtime 当前通过 `build_default_tools` 构建内置工具，并通过 `ConfigService` 解析工具策略和 `confirmation_required`。扩展工具链已有固定的 `invoke_custom_tool` 入口；本变更需要把 `NodeDebugService` 注入扩展工具 factory，而不是把调试目标工具加入默认工具列表。

本变更需要把已有 Node 调试能力映射为 Agent 工具，同时保留未来 adapter 的边界。当前不引入 VS Code 扩展或 debugpy 依赖，也不把调试端口、线程和 frame 标识交给模型。

## Goals / Non-Goals

**Goals:**

- 通过 `tools.custom` 扩展工具组注册 DebugMCP 风格的 16 个执行工具和 4 个会话方案工具，并只向模型暴露固定的 `invoke_custom_tool` 入口。
- 将工具调用的 session、workspace、tool call identity 和配置依赖由闭包/运行时容器注入。
- 为 Node Inspector 实现生命周期、执行控制、普通/条件断点、变量检查和表达式求值。
- 为 Node Inspector 实现命中次数断点和不会暂停目标程序的 logpoint，并将输出纳入调试控制台。
- 将 `runtime.debug` 接入现有 Workspace JSONC 合并和 schema 校验。
- 使用统一 JSON 文本结果，兼容现有 Agent tool output middleware，同时保持完整调试快照。
- 通过现有工具策略支持调试工具禁用和表达式求值人工确认。
- 用纯后端 E2E 验证工具目录、真实 Node fixture、session 隔离、断点控制、变量/表达式和配置覆盖。

**Non-Goals:**

- 本次不实现 Python debugpy adapter、通用 DAP adapter 或 VS Code Debug API 路由。
- 本次不支持 Agent 选择任意 thread/frame，不支持修改运行时变量。
- 本次不允许 Agent 通过 Inspector/debugpy 端口连接外部进程。
- 本次不改变 Web 调试面板的整体布局；在现有源码行号槽增加共享的特殊断点右键菜单。
- 本次不把 `runtime.debug` 的 `program`、`runtime` 或 `adapter` 直接加入 Agent 工具输入。

## Decisions

### 1. Use custom tools and the fixed invocation boundary

在 `app/agents/tools/debugging.py` 中提供 20 个目标 `StructuredTool` factory，并通过 `tools.custom` 配置逐项注册。`build_custom_tool_bundle` 将它们构建为扩展目标工具，再由固定的 `invoke_custom_tool` 进行二次分发；`build_default_tools` 不接收或注册调试工具。

工具组是否可调用仍由现有 `denylist`、`allowlist` 和 `confirmation_required` 解析；`evaluate_expression` 不做特殊的绕过路径。这样既满足 DebugMCP schema 兼容性，也沿用当前扩展工具策略的一致行为。工具目录展示 20 个目标工具的能力信息，但模型工具 schema 只包含固定入口。

目标工具直接注册到默认 runtime 的方案会绕过扩展工具的隔离边界，也会让模型工具列表随扩展工具组膨胀，因此不采用。

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

沿用现有 `NodeDebugStateDTO`、断点、调用栈、求值和动作模型，补充 `hit_condition`、condition/logpoint 元数据和工具调用审计字段。普通/条件断点使用 Inspector `Debugger.setBreakpointByUrl` 的 condition 能力。

Node Inspector 没有与 VS Code `SourceBreakpoint.logMessage` 等价的独立 API，因此 Node adapter 将命中次数、用户条件和日志表达式编译为 Inspector 条件表达式。命中计数保存在目标进程的内部 Symbol 键空间中，每次目标进程重启后清零；命中次数断点只在指定的第 N 次命中时暂停。日志点在条件满足时写入带内部前缀的 stdout 并始终返回 false，因此不会暂停；后端剥离前缀后将内容作为普通调试输出展示。日志消息支持 `{expression}` 插值，未闭合或空表达式在安装前明确报错。

同一路径、行和列只允许一个断点定义，类型由 `condition`、`hit_condition` 和 `log_message` 的组合决定。Web 编辑特殊断点时使用原子更新动作，避免先删后加导致界面短暂丢失；Agent 的 `add_breakpoint` 与 `add_logpoint` 仍使用新增语义，并在位置已占用时获得明确错误。

### 6. Return JSON text for LangChain compatibility

当前 Agent 工具和 ToolOutputMiddleware 已经稳定处理字符串结果，因此工具返回 `json.dumps` 生成的 JSON 文本：成功为 `{"ok":true,"message":...,"state":...}`，失败为 `{"ok":false,"error":{"code":...,"message":...},"state":...}` 或抛出带明确错误的工具异常。HTTP API 继续返回现有 `APIResponse`，不把 Agent 的 JSON 文本格式反向强加给 Web API。

### 7. Test the real backend without browser dependencies

新增 `tests/e2e/backend/agents/test_debug_tools.py`，使用 E2E fixture workspace 写入一个稳定的 JS 调试 fixture，通过 Agent tool factory 直接 `ainvoke` 工具，并检查真实 Node 进程状态、断点停靠、变量、表达式、单步、清理和 session 隔离。该部分是 Node Inspector 适配器集成检查，不作为提示词驱动 E2E 的替代。

测试还通过临时 workspace `.boxteam/workspace.jsonc` 验证 `runtime.debug` profile 覆盖和配置 schema；不启动 Web、Gateway 或 VS Code，不运行 `reference_repo` 测试。

### 8. Add the product Skill and model-driven verification

产品级调试说明唯一维护在 `resources/skills/debugging/`，包含 20 个扩展目标工具的 `tool_name` + `arguments_schema` 契约、通过 `invoke_custom_tool` 调用的方式、基于 `state.status` 的最少调用决策树、并发操作处理、断点到结束的实际检查点、暂停上下文约束和 Inspector 内部字段脱敏规则。提示词优先告诉模型“先看状态再行动”：已有运行时就复用，人类推进后接受最新状态，`invalid_breakpoints` 只提醒不阻断。默认 E2E 工作区准备器把 `resources/skills/` 复制到隔离工作区的 `/.boxteam/skills`，因此 `asset/` 不维护一份会漂移的产品 Skill 副本。

新增 `test_debug_prompt_flow.py` 启动真实 Workspace 后端，通过本地 OpenAI-compatible HTTP 服务返回确定性的模型 tool calls。它必须经过用户 session prompt、Skill `read_file`、调试工具调用、真实 Node Inspector 状态和最终 assistant marker；这样验证的是完整 Agent/model 协议边界，而不是直接调用工具 factory。另有 `test_debug_prompt_live.py`，仅在 `BOXTEAM_RUN_LIVE_DEBUG_E2E=1` 时使用当前配置的外部模型，验证真实模型是否遵守同一流程。

### 9. Use shared-operation semantics instead of ownership handoff

源码调试运行时按 session 绑定，本身已经可以由 Web API 和 Agent 工具共同访问。两类入口共享状态锁和 Inspector command lock，动作完成后都返回完整 state，因此不再叠加 `ai`、`human`、`collaborative` 控制模式。Actor 只用于审计，不是权限字段。

用户发来的新会话消息和 Web 控制动作可以改变源码调试运行时；Skill 要求模型接受这种乐观并发，并在下一次动作前读取最新权威状态。

### 10. Persist portable multi-configuration state and reconcile source anchors lazily

会话级 Node debug store 在 `SessionPathResolver.resolve_session_node(session_id)` 下保存会话 manifest、动作审计和多套独立方案文件。manifest 只记录活动方案与会话级审计；每套方案文件包含稳定方案 ID、显示名、启动选择、方案专用断点、源码锚点、schema 版本和修订号，不包含 session ID、动作历史、Inspector URL、PID、调用栈、变量对象、Inspector 断点 ID 或运行时验证状态。

方案中的路径全部相对当前工作区。后端通过枚举并严格校验 `debug/node/configurations/*.json` 发现方案，因此把单个方案文件复制到另一会话的同名目录即可使用；HTTP API 另提供跨会话复制。旧的单文件 `debug/node.json` 不读取、不迁移，也不提供兼容 DTO。

源码变化使用 reconciliation hook：`get_state`、控制动作以及 start/restart 都会经过同一校验。Web 已定时读取状态，Agent 每个工具也会读取权威状态，因此无需给 NodeDebugService 再创建常驻文件 watcher。fallback 策略不尝试按锚点重定位：关联文件内容变化后保留原请求行号并标记 `pending_update`，删除文件时标记 `source_deleted`，同时移除活动 Inspector 中的旧断点映射。活动进程发生任何关联源码变化都设置 `requires_restart` 和 `source_changed_paths`，因为 Node 已加载的源码不会随磁盘文件自动更新；这些标记只用于提醒，不阻止继续、暂停、单步或停止。只有显式重新设置断点才恢复为 `current`。`start_debugging` 和调试结束时的最后一个成功控制工具结果额外返回 `invalid_breakpoints`，让 Agent 在两个关键边界集中处理提醒。

### 11. Follow actual paused frames across JavaScript modules

测试 fixture 拆分为入口模块和被导入模块，并在两者设置断点。后端继续以 Inspector call frame URL 映射工作区路径；Web 暂停时优先显示顶层 frame，未暂停时选择最近新增或用户点击的断点。这样窄体预览保持低交互，而扩展窗口仍可打开完整文件。

### 12. Remove Agent execution-loop debugging completely

源码调试工具用于 Agent 替用户调试目标 JavaScript 程序，不用于观察或暂停 BoxTeam Agent 自身的 LLM 请求、思考、工具调用生命周期。删除 `tool_before`、`tool_after`、`llm_before` 断点、`debug_stop` / `debug_action` 轨迹、Job 调试控制动作、运行时暂停门和 Web 的 Agent 执行标签页。删除后不保留兼容枚举、空实现、隐藏开关或旧路由。

### 13. Drive conversational debugging from saved schemes and authoritative stops

自然语言调试请求不要求用户先手工填写方案。Agent 先读取工作区文件定位入口与依赖，再列举会话方案；只有目标和现有方案不匹配时创建具名方案。调试中的解释以每次工具返回的实际顶层 frame 为依据，不能预先生成一串假定断点说明。用户要求修改计数时使用暂停上下文中的 `evaluate_expression("counter += 1")`，该动作进入同一调试控制台和审计时间线，但不修改源码文件。

右侧侧边栏只呈现权威调试状态：初始没有可跟随位置时显示空状态；方案或断点出现后展示静态预览；暂停后始终由顶层 frame 覆盖旧选择；退出后保留最后位置、输出和动作，允许用户使用同一方案再次手动调试。

## Risks / Trade-offs

- [日志点依赖 Inspector 条件表达式副作用] → 生成表达式始终返回 false，输出使用内部前缀识别，并用真实 Node fixture 验证日志产生且程序不会暂停。
- [命中计数可能跨重启产生歧义] → 计数只存在于当前目标进程，restart/start 明确从 0 重新计数，方案文件只保存目标次数。
- [表达式求值可以执行副作用代码] → 复用工具确认策略、强制记录 tool call identity，并保持 Inspector loopback/session 绑定。
- [扩展目标工具不能直接出现在模型工具列表] → 工具目录使用独立 `debugging` 分组，模型只看到固定 `invoke_custom_tool`；工具策略仍可整体或逐项 denylist。
- [配置 profile 可能与当前 Node 直接启动参数冲突] → 先定义规范化 profile 解析，未配置时完全回退到现有 Node 默认行为；固定端口和外部 adapter 不默认启用。
- [运行时状态与异步 Inspector 事件存在竞态] → 所有工具动作等待后端 authoritative snapshot；变量 hydration 完成后才返回暂停快照；E2E 保持暂停状态并验证重复读取。
- [错误结果需要同时满足 Agent 和现有工具错误处理] → 使用稳定错误 code 的 JSON 文本并保留异常边界；不返回虚假默认状态。

## Migration Plan

1. 更新 Workspace schema 和 inline config，新增可选的 `runtime.debug` 默认配置，不改旧用户配置文件。
2. 更新 Agent 工具全集、目录分组和运行时依赖注入；默认 Node profile 使用现有动态 Inspector 行为。
3. 部署后，未配置 `runtime.debug` 的工作区继续使用 Node 默认值；没有 Node 可执行文件时工具返回明确错误。
4. 如需回滚，移除调试工具配置/denylist 入口并停止 Agent 创建的 Node runtime；已有 HTTP Node 调试 API 保持可用。
5. 未来增加 debugpy 或 VS Code adapter 时，只新增 adapter 实现和 profile 校验，不改变现有 20 个 Agent 工具的输入 schema。
