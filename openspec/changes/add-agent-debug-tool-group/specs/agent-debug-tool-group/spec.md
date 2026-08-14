## Purpose

为工作区 Agent 提供一组稳定、可审计且尽量兼容 DebugMCP 的源码调试工具，使 Agent 能在当前会话中启动程序、控制执行、管理断点并检查暂停时的运行时状态。

## ADDED Requirements

### Requirement: Agent exposes a compatible debugging tool group

系统 SHALL 在启用该能力的扩展工具组中注册以下 16 个 DebugMCP 兼容执行工具：

```text
start_debugging
stop_debugging
step_over
step_into
step_out
continue_execution
pause_execution
restart_debugging
add_breakpoint
add_logpoint
remove_breakpoint
clear_all_breakpoints
list_breakpoints
list_variable_names
get_variables_values
evaluate_expression
```

系统还 SHALL 注册 `list_debug_configurations`、`create_debug_configuration`、`activate_debug_configuration`、`delete_debug_configuration` 4 个会话方案管理工具。

工具的模型可见输入 schema SHALL 不包含 `session_id`、`job_id`、`adapter`、`launch`、`runtime`、`program`、`inspectorPort`、`debugpyPort`、`vscodeSessionId`、`threadId` 或 `frameId` 等后端控制字段；会话身份和基础设施依赖必须由当前 Agent 工具上下文注入。

这些目标工具 SHALL 只能通过固定的 `invoke_custom_tool` 入口调用。Agent runtime 的模型工具列表 SHALL 暴露 `invoke_custom_tool`，而不得把上述 20 个目标工具作为独立模型工具直接注册；`invoke_custom_tool.arguments` SHALL 使用 `tool_name` 和目标工具的 `arguments` 承载调用。

#### Scenario: Agent receives the compatible extension tool group

- **WHEN** Agent 使用包含调试工具组的有效工具配置创建运行时
- **THEN** 工具目录包含上述 20 个目标工具，模型工具列表包含 `invoke_custom_tool`，且不包含上述目标工具的独立模型入口

#### Scenario: Backend identity fields stay hidden

- **WHEN** 系统导出任一调试工具的模型 schema
- **THEN** schema 不要求 Agent 提供当前 session 或后端连接标识

### Requirement: Custom target input schemas match the DebugMCP contract

系统 SHALL 将以下输入契约作为扩展目标工具的 `invoke_custom_tool.arguments` schema；除必需性和取值约束外，不得为了当前后端实现增加新的目标参数。固定入口的外层 schema 由 Agent runtime 的 `invoke_custom_tool` 工具定义提供。

```json
{
  "start_debugging": {
    "fileFullPath": "string, required",
    "workingDirectory": "string, required",
    "testName": "string, optional",
    "configurationName": "string, optional"
  },
  "stop_debugging": {},
  "step_over": {},
  "step_into": {},
  "step_out": {},
  "continue_execution": {},
  "pause_execution": {},
  "restart_debugging": {},
  "add_breakpoint": {
    "fileFullPath": "string, required",
    "line": "integer, required, 1-based",
    "condition": "string, optional",
    "hitCondition": "integer, optional, >= 1"
  },
  "add_logpoint": {
    "fileFullPath": "string, required",
    "line": "integer, required, 1-based",
    "logMessage": "string, required",
    "condition": "string, optional",
    "hitCondition": "integer, optional, >= 1"
  },
  "remove_breakpoint": {
    "fileFullPath": "string, required",
    "line": "integer, required, 1-based"
  },
  "clear_all_breakpoints": {},
  "list_breakpoints": {},
  "list_variable_names": {
    "scope": "local | global | all, optional"
  },
  "get_variables_values": {
    "variableNames": "string[], required, 1-50 items",
    "scope": "local | global | all, optional"
  },
  "evaluate_expression": {
    "expression": "string, required"
  }
}
```

`line` SHALL 是从 1 开始的正整数；`hitCondition` SHALL 是正整数；`variableNames` SHALL 至少包含一个且最多包含 50 个非空名称；`scope` SHALL 只能取 `local`、`global` 或 `all`。

#### Scenario: Agent starts a source debug session

- **WHEN** Agent 通过 `invoke_custom_tool` 以 `tool_name=start_debugging`，并使用 `fileFullPath` 和 `workingDirectory` 作为 `arguments` 调用
- **THEN** 系统根据当前工作区解析目标文件，使用选定的调试启动配置创建会话，并返回调试状态

#### Scenario: Saved configuration is authoritative when starting

- **WHEN** 当前会话已有活动方案，或 `start_debugging` 显式选择一套方案
- **THEN** 系统 SHALL 使用该方案保存的目标文件、工作目录、profile、参数和断点启动，不得被启动请求中的临时值静默覆盖

#### Scenario: Invalid tool arguments are rejected

- **WHEN** Agent 传入缺少必填字段、非正整数行号、空变量数组或不支持的 scope
- **THEN** 工具调用失败并返回明确的参数错误，不启动或改变调试会话

### Requirement: Debugging actions are scoped to the current Agent session

系统 SHALL 将每个调试运行时绑定到当前 Agent session；同一 session 的后续控制、断点和检查工具 SHALL 只访问该 session 的调试运行时。工具不得根据 Agent 提供的端口、WebSocket 地址或 VS Code 会话 ID 跨 session 选择调试目标。

#### Scenario: Actions use the current session runtime

- **WHEN** Agent 在已有调试会话中调用 `continue_execution`、单步、断点或检查工具
- **THEN** 系统只控制当前 Agent session 对应的调试进程

#### Scenario: No active session

- **WHEN** Agent 在未启动调试会话时调用需要活动调试上下文的工具
- **THEN** 工具失败并返回可识别的无活动会话错误，不返回虚假成功状态

#### Scenario: Starting again replaces the current session runtime safely

- **WHEN** Agent 在同一 session 已有运行中的调试会话时调用 `start_debugging`
- **THEN** 系统先停止并清理旧运行时，再创建新的调试运行时，并且不影响其他 session

### Requirement: Execution controls report authoritative debug state

系统 SHALL 支持 `stop_debugging`、`restart_debugging`、`continue_execution`、`pause_execution`、`step_over`、`step_into` 和 `step_out`。每个成功的控制动作 SHALL 返回后端确认后的完整调试状态，而不是只返回本地预期状态。

调试状态至少 SHALL 能表达：`idle`、`starting`、`running`、`paused`、`exited` 和 `failed`，并在适用时包含目标脚本、暂停原因、调用栈、断点、输出、最近一次求值和动作记录。模型工具结果 SHALL 移除 session ID、PID、Inspector 断点 ID、call frame ID、object ID、tool call ID 等后端路由字段；这些字段只允许存在于后端和 Web 内部 API。

#### Scenario: Agent continues from a breakpoint

- **WHEN** 当前会话暂停且 Agent 调用 `continue_execution`
- **THEN** 程序继续执行，工具返回真实的 running、paused、exited 或 failed 状态及其最新快照

#### Scenario: Agent pauses a running program

- **WHEN** 当前会话正在运行且 Agent 调用 `pause_execution`
- **THEN** 系统请求目标程序暂停，并在成功暂停后返回当前调用栈和暂停位置

#### Scenario: Agent steps from a paused frame

- **WHEN** 当前会话暂停且 Agent 调用任一单步工具
- **THEN** 系统执行对应单步动作，并返回新的暂停位置、调用栈和变量快照

#### Scenario: Control is invalid for the current state

- **WHEN** Agent 在已退出、失败或没有暂停上下文时调用不适用的控制动作
- **THEN** 工具返回明确错误和当前真实状态，不伪造单步或继续成功

### Requirement: Breakpoint and logpoint operations are observable

系统 SHALL 支持普通断点、条件断点、命中次数断点和 logpoint 的新增、编辑、移除、列举与全部清理。断点状态 SHALL 至少包含路径、请求行号、实际绑定行号（如果适用）、是否已验证、条件、命中次数和日志表达式信息。

对于底层 adapter 不支持某类断点的情况，系统 SHALL 明确返回不支持错误或等价的可观察结果，不得静默将 logpoint 当作普通暂停断点。

#### Scenario: Agent adds a verified breakpoint

- **WHEN** Agent 使用有效路径和正整数行号调用 `add_breakpoint`
- **THEN** 系统登记该断点，尽可能安装到底层调试器，并在返回状态中报告 verified 和实际绑定位置

#### Scenario: Agent lists and removes a breakpoint

- **WHEN** Agent 调用 `list_breakpoints` 后使用对应路径和行号调用 `remove_breakpoint`
- **THEN** 列表反映当前断点集合，移除成功后该断点不再出现在列表中

#### Scenario: Agent requests a conditional breakpoint

- **WHEN** Agent 为 `add_breakpoint` 提供 condition
- **THEN** 系统将条件传递给调试后端，或返回明确说明当前 adapter 不支持条件断点的错误

#### Scenario: Agent requests a hit-count breakpoint

- **WHEN** Agent 为 `add_breakpoint` 提供正整数 `hitCondition`
- **THEN** Node adapter 仅在当前目标进程第 N 次到达该位置且可选 condition 同时为真时暂停；重新启动目标进程后从第一次命中重新计数

#### Scenario: Agent requests a logpoint

- **WHEN** Agent 调用 `add_logpoint`
- **THEN** Node adapter 在可选 condition 和 hitCondition 满足时将插值后的 logMessage 写入调试输出，并继续执行而不暂停；其他 adapter 必须安装等价 logpoint 或返回明确的不支持错误

#### Scenario: Human edits a special breakpoint from a source gutter

- **WHEN** 用户在右侧侧边栏源码预览或扩展窗口源码区右键点击行号槽
- **THEN** Web 展示普通断点、条件断点、命中次数断点和日志点选项，并允许对该行已有断点编辑或删除；左键仍快速切换普通断点

#### Scenario: One source location has one authoritative breakpoint

- **WHEN** 同一路径、行和列已经存在任意类型断点
- **THEN** 新增操作返回明确的位置占用错误，编辑操作原子替换该定义，Web 和 Agent 随后读取到同一份权威状态

### Requirement: Paused state supports variable inspection and evaluation

系统 SHALL 在有效暂停上下文中支持 `list_variable_names`、`get_variables_values` 和 `evaluate_expression`。变量工具 SHALL 遵守 scope 和名称限制；求值结果 SHALL 包含表达式、结果值或类型信息，并在目标运行时抛出异常时返回可识别的求值错误。

系统 SHALL 将 `evaluate_expression` 视为高风险调试动作：每次调用必须可审计，并 SHALL 通过现有工具确认策略支持按工具名要求人工确认。

#### Scenario: Agent inspects local variables

- **WHEN** 程序暂停且 Agent 请求 local scope 的变量名或指定变量值
- **THEN** 系统返回当前暂停 frame 中可见的变量信息，不读取未请求的变量值

#### Scenario: Agent evaluates an expression

- **WHEN** 程序暂停且 Agent 调用 `evaluate_expression`
- **THEN** 系统在当前暂停上下文执行表达式，返回求值结果或目标运行时错误，并记录表达式和调用身份

#### Scenario: Evaluation without a paused frame

- **WHEN** 程序未暂停或没有有效调用栈时 Agent 请求变量值或表达式求值
- **THEN** 工具失败并说明需要有效暂停上下文

### Requirement: Tool results and failures are structured and auditable

每个调试工具 SHALL 返回统一的成功或失败结果。成功结果 SHALL 包含 `ok: true`、可读 `message` 和最新调试状态；失败结果 SHALL 包含 `ok: false`、稳定错误 code 和详细 message。工具失败不得返回表示成功的默认状态。

每个调试动作，包括 Agent 发起的 start、stop、continue、pause、step、断点变更、变量读取和表达式求值，SHALL 进入当前调试会话的动作时间线，并包含工具名、session、调用身份、时间和结果。

#### Scenario: Successful tool result replaces stale state

- **WHEN** 调试动作成功
- **THEN** Agent 和 Web 消费到包含完整最新状态的结果，不能只收到局部字段补丁

#### Scenario: Failed tool result is diagnosable

- **WHEN** 底层调试器、路径解析或表达式求值失败
- **THEN** 工具返回失败 code、明确原因和当前可获得的真实状态，不能吞掉异常

### Requirement: Debugging Skill is discoverable and the prompt flow is verifiable

系统 SHALL 在 `resources/skills/debugging/SKILL.md` 提供与 20 个扩展目标工具同步的 `tool_name` + `arguments_schema` JSON 契约、通过 `invoke_custom_tool` 调用的方式、面向模型的状态决策流程、并发处理规则和安全边界。该流程 SHALL 指导模型优先读取权威状态、只在必要时追加工具调用，并明确 `invalid_breakpoints` 的出现时机及处理方式；不得要求模型机械重复查询或为取得控制权而重启。E2E 工作区 SHALL 从该产品资源复制 Skill 到 `/.boxteam/skills/debugging/SKILL.md`，不得维护一份会漂移的产品副本。

#### Scenario: Agent reads the debugging Skill before acting

- **WHEN** 用户通过 session message 请求调试 JavaScript 源码
- **THEN** 提示词驱动测试可以观察到 Agent 先读取 `/.boxteam/skills/debugging/SKILL.md`，再通过 `invoke_custom_tool` 调用 `add_breakpoint`、`start_debugging` 和其他调试目标工具

#### Scenario: Prompt-driven flow reaches a real Node debug session

- **WHEN** 测试模型通过 OpenAI-compatible HTTP 接口从用户 prompt 返回调试工具调用
- **THEN** 真实 Workspace 后端执行这些调用，Node Inspector 返回暂停、求值、单步和结束状态，最终 assistant message 返回稳定完成标记

#### Scenario: Model follows authoritative state branches

- **WHEN** 工具结果分别返回 `idle`、`running`、`paused`、`exited`、`failed`，或在开始/结束结果中返回 `invalid_breakpoints`
- **THEN** Skill 让模型按状态选择最少的下一步：运行中不读取变量，暂停后依据真实 frame 分析，失效断点只作为提醒并不阻断控制，结束后才报告结果；模型不重复启动、不虚构断点命中，也不把普通状态查询当作结束反馈

#### Scenario: External model verification is explicit

- **WHEN** 设置 `BOXTEAM_RUN_LIVE_DEBUG_E2E=1` 运行 live E2E
- **THEN** 测试使用当前 Workspace provider 发送同一调试 prompt，并断言真实模型生成了读取 Skill、设置断点、调试控制和完成顺序；未设置时不得隐式调用外部模型

### Requirement: Human and Agent share one debug session without ownership transfer

系统 SHALL 允许人类通过 Web 控件和 Agent 通过调试工具并列操作当前 Agent session 绑定的同一个源码调试运行时。调试协议不得要求 `takeover`、`handoff` 或控制模式切换；任一方的继续、暂停、单步、断点和求值动作 SHALL 进入同一动作时间线，并返回动作完成后的完整权威状态。

Debugging Skill SHALL 明确说明：人类可能在 Agent 两次工具调用之间继续、暂停、单步、停止、修改断点、在终端输入或修改源码。Agent SHALL 把工具返回的最新 state 作为事实；发现状态已由人类推进时继续分析，不得尝试取得权限或重置已有调试会话。

#### Scenario: Human advances a model-started debug session

- **WHEN** Agent 启动调试并暂停后，人类在 Web 中点击继续或单步
- **THEN** 后续 Agent 工具读取到人类动作后的最新状态，并可从该状态继续调试，无需任何交接动作

#### Scenario: Agent continues a human-started debug session

- **WHEN** 人类在当前会话启动源码调试后发送消息要求 Agent 继续检查
- **THEN** Agent 使用当前 session 的已有调试状态和断点，不为取得控制权而重启或替换该运行时

### Requirement: Cross-file stops expose the actual source location

系统 SHALL 允许同一调试配置在多个工作区 JavaScript 文件上保存断点。暂停快照的顶层调用栈 frame SHALL 返回实际暂停文件和行号；Web 源码预览 SHALL 优先跟随该 frame，并在未暂停时跟随最近选择或新增的断点。

#### Scenario: Execution moves from entry module to imported module

- **WHEN** 调试入口文件调用另一个 JavaScript 模块并依次命中两个文件中的断点
- **THEN** 每次暂停的调用栈、窄体源码预览和扩展窗口当前源码位置都切换到实际命中的文件和行

### Requirement: Conversation-driven debugging explains and mutates each stop

系统 SHALL 支持用户仅给出自然语言目标后，由 Agent 从工作区源码中定位 JavaScript 入口和相关模块。Agent SHALL 先列举当前会话方案；没有适合该目标的方案时创建具名方案，在相关文件设置断点并启动。每次真实暂停后，Agent SHALL 根据最新顶层 frame 说明该处代码的作用，再按用户要求通过 `evaluate_expression` 修改暂停帧中的计数变量，然后才继续到下一断点。表达式求值只修改目标进程运行时，不得改写工作区源码。

Web 右侧侧边栏的源码预览在没有活动文件、方案或暂停位置时 SHALL 显示明确空状态；创建方案或断点后可以显示对应源码；每次暂停时 SHALL 以最新顶层 frame 为最高优先级切换文件和当前行；程序结束后 SHALL 保留最后调试上下文和退出状态，供用户复查和重新手动启动。

#### Scenario: Agent creates a missing configuration and debugs across files

- **WHEN** 用户要求模型调试刚创建的入口文件及相关模块，而当前会话没有名称或目标匹配的调试方案
- **THEN** Agent 先列举方案和断点，再创建具名方案、设置跨文件断点、启动调试，并在每次暂停后按“解释、求值计数变量加一、继续”的顺序工作直到真实退出

#### Scenario: Sidebar source preview follows consecutive stops

- **WHEN** 模型或用户让运行时从入口文件断点继续到相关模块断点
- **THEN** 右侧侧边栏源码预览从空状态进入源码状态，并在每次暂停后显示实际文件、断点行和当前执行行，不停留在上一文件

#### Scenario: Human reruns the saved model configuration

- **WHEN** 模型调试已退出且用户使用同一会话活动方案在 Web 点击启动、继续或单步
- **THEN** 用户可以复用模型保存的入口、参数和跨文件断点逐步执行到退出，所有按钮状态、源码预览、控制台与动作历史保持一致

### Requirement: Breakpoints reconcile with changed source

系统 SHALL 为会话源码断点保存足以识别原代码位置的源码锚点。读取调试状态、执行调试动作或启动/重启前 SHALL 检查关联文件版本；只要关联文件内容发生变化，相关断点 SHALL 保留原请求行号并标记为 `pending_update`，文件删除时标记为 `source_deleted`，不得自动重定位或静默将旧行号安装到新源码。

活动调试进程加载源码后文件发生变化时，状态 SHALL 标记 `requires_restart` 和 `source_changed_paths`，并移除 Inspector 中相关的旧断点映射。源码变化不得阻止 `continue_execution`、`pause_execution`、`step_*` 或 `stop_debugging`；目标进程可以继续执行已经加载的代码。只有 Agent 显式重新设置断点后，该断点才恢复为 `current` 并允许安装。

`start_debugging` 的成功结果以及导致调试状态变为 `exited` 或 `failed` 的最后一个成功控制工具结果 SHALL 顶层包含 `invalid_breakpoints` 数组，列出路径、原请求行号、状态和提醒信息；其他工具不额外返回该顶层字段，但其完整 `state.breakpoints` 仍保留失效状态。

#### Scenario: Lines are inserted before a breakpoint

- **WHEN** 会话已保存断点，随后在断点源码之前插入若干行，且原源码锚点仍可唯一识别
- **THEN** 系统不改变断点请求行号，将其标记为 `pending_update`，不安装该断点；继续或单步仍可驱动当前已加载的目标进程，Agent 在启动结果或最终结果中看到该断点的 `invalid_breakpoints` 提醒

#### Scenario: Breakpoint anchor becomes ambiguous

- **WHEN** 源码变化后存在多个同等匹配位置或原文件已删除
- **THEN** 断点标记为待更新或源文件已删除，并保持未验证状态，不安装到猜测位置；该失效状态不阻断调试控制动作
