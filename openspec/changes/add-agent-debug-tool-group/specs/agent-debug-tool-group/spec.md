## Purpose

为工作区 Agent 提供一组稳定、可审计且尽量兼容 DebugMCP 的源码调试工具，使 Agent 能在当前会话中启动程序、控制执行、管理断点并检查暂停时的运行时状态。

## ADDED Requirements

### Requirement: Agent exposes a compatible debugging tool group

系统 SHALL 向启用该能力的 Agent 暴露以下 16 个独立工具名称，并 SHALL 保持这些工具名称使用 snake_case：

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

工具的模型可见输入 schema SHALL 不包含 `session_id`、`job_id`、`adapter`、`launch`、`runtime`、`program`、`inspectorPort`、`debugpyPort`、`vscodeSessionId`、`threadId` 或 `frameId` 等后端控制字段；会话身份和基础设施依赖必须由当前 Agent 工具上下文注入。

#### Scenario: Agent receives the compatible tool names

- **WHEN** Agent 使用包含调试工具组的有效工具配置创建运行时
- **THEN** 工具目录包含上述 16 个工具，并且每个工具可以被模型按独立工具名调用

#### Scenario: Backend identity fields stay hidden

- **WHEN** 系统导出任一调试工具的模型 schema
- **THEN** schema 不要求 Agent 提供当前 session 或后端连接标识

### Requirement: Tool input schemas match the DebugMCP contract

系统 SHALL 使用以下输入契约；除必需性和取值约束外，不得为了当前后端实现增加新的模型可见字段。

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
    "condition": "string, optional"
  },
  "add_logpoint": {
    "fileFullPath": "string, required",
    "line": "integer, required, 1-based",
    "logMessage": "string, required",
    "condition": "string, optional"
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

`line` SHALL 是从 1 开始的正整数；`variableNames` SHALL 至少包含一个且最多包含 50 个非空名称；`scope` SHALL 只能取 `local`、`global` 或 `all`。

#### Scenario: Agent starts a source debug session

- **WHEN** Agent 使用 `fileFullPath` 和 `workingDirectory` 调用 `start_debugging`
- **THEN** 系统根据当前工作区解析目标文件，使用选定的调试启动配置创建会话，并返回调试状态

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

调试状态至少 SHALL 能表达：`idle`、`starting`、`running`、`paused`、`exited` 和 `failed`，并在适用时包含目标脚本、进程信息、暂停原因、调用栈、断点、输出、最近一次求值和动作记录。

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

系统 SHALL 支持普通断点、条件断点和 logpoint 的新增、移除、列举与全部清理。断点状态 SHALL 至少包含路径、请求行号、实际绑定行号（如果适用）、是否已验证以及条件或日志表达式信息。

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

#### Scenario: Agent requests a logpoint

- **WHEN** Agent 调用 `add_logpoint`
- **THEN** 系统安装真正的 logpoint，或返回明确的不支持错误；系统不得悄悄把它降级为会暂停程序的普通断点

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
