## Purpose

为工作区Agent提供一组稳定、可审计且尽量兼容DebugMCP的源码调试目标；模型只通过固定`invoke_extension_tool`信封调用它们，所有调试资源精确属于发起调用的SessionThread。

## ADDED Requirements

### Requirement: DebugMCP风格目标通过固定信封暴露，而非16个Provider直接工具

系统 SHALL在ExtensionToolCatalog的`debugging`分组登记以下16个稳定、snake_case目标名称；这些名称只作为`invoke_extension_tool(tool_name, arguments)`的`tool_name`值，不得作为16个独立Provider工具定义。Provider `tools`只包含当前capability profile选出的少量直接工具和始终存在、名称/schema/description固定的`invoke_extension_tool`；信封description不得列出目标名或目标schema。现有调试方案管理目标也须使用同一信封与thread owner，不借此规格把它们算作16个DebugMCP核心目标：

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

固定信封的模型可见输入只有`tool_name`和`arguments`。各调试目标的公开参数schema SHALL不包含`session_id`、产品`thread_id`、`job_id`、`adapter`、`launch`、`runtime`、`program`、`inspectorPort`、`debugpyPort`、`vscodeSessionId`、DAP`threadId`或`frameId`等后端控制字段；精确SessionThread、原model-call目录binding和基础设施依赖必须由受信Agent工具上下文注入。模型通过受控bundled调试Skill或同等级来源指引了解目标名称/参数；该指引不改变Provider信封schema、ToolSetRef或执行权限。

#### Scenario: Agent receives the compatible tool names

- **WHEN** Agent使用包含调试目标组的有效配置创建运行时
- **THEN** 内层ExtensionToolCatalog包含上述16个目标且以稳定名称接受信封分发；Provider `tools`只含一个固定`invoke_extension_tool`定义而不直接含这16个名称，目标目录改变不自动创建`toolset_changed` epoch

#### Scenario: Backend identity fields stay hidden

- **WHEN** 系统导出Provider信封和任一调试目标的公开参数schema
- **THEN** 信封只含`tool_name/arguments`，目标参数schema不要求模型提供session、产品thread或后端连接标识；来源指引可描述目标参数，但不得把目标schema并入信封description

#### Scenario: 调试目标启停不破坏已提交前缀

- **WHEN** 调试目标在两个model call间被允许、禁用或更新描述，但Provider直接工具与固定信封未变化
- **THEN** 后一次请求只在既定资源激活边界选中新的ExtensionCatalogBindingRef及必要user-role指引，原ToolSetRef和prefix epoch不变；已经sealed的调用继续按原binding解析并在执行点重验最新权限

### Requirement: 信封内目标参数schema保持DebugMCP风格契约

系统 SHALL对`invoke_extension_tool.arguments`按`tool_name`选择以下目标参数契约做后端严格校验；除必需性和取值约束外，不得为了当前后端增加模型必须提供的owner或连接字段。此JSON是内层目标schema，不是Provider `tools`中的16个定义。

```json
{
  "start_debugging": {
    "fileFullPath": "string, required",
    "workingDirectory": "string, required",
    "testName": "string, optional",
    "configurationName": "string, optional",
    "debugConfigurationId": "string, optional"
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
    "hitCondition": "integer, optional, 1-based"
  },
  "add_logpoint": {
    "fileFullPath": "string, required",
    "line": "integer, required, 1-based",
    "logMessage": "string, required",
    "condition": "string, optional",
    "hitCondition": "integer, optional, 1-based"
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

`line`和`hitCondition` SHALL是从1开始的正整数；`variableNames` SHALL至少包含一个且最多包含50个非空名称；`scope` SHALL只能取`local`、`global`或`all`。信封本身仍只暴露固定的`tool_name/arguments`；所有目标参数须经绑定目标的严格schema校验，未知字段、错误类型、非法取值或多余外层字段明确失败，不允许忽略后继续执行。

启动方案选择 SHALL按以下固定顺序进行：显式`debugConfigurationId`→当前受信thread的活动方案→当前thread无方案时以已验证的`fileFullPath`和`workingDirectory`创建方案。`debugConfigurationId`只在调用的`ThreadRuntimeBinding`所指thread目录内解析；任何未命中统一返回`debug_configuration_not_found`，不查询其它thread、不回退其它方案；`configurationName`只选择Workspace launch profile，不是可跨thread选择方案的ID。已选保存方案的入口、工作目录、参数及断点优先，但两个必填路径仍须规范化并与该方案按当前有效Workspace profile解析出的实际入口/工作目录相等；显式传入的`configurationName`也须等于方案实际解析出的profile。任一已知参数冲突返回带字段信息的`debug_launch_parameter_conflict`，不覆盖方案、不静默忽略、不激活新方案、不停止旧进程或启动新进程；未传`configurationName`不构成冲突。若模型需要改变方案，须先显式创建/激活新方案。显式ID未命中的错误先于路径/profile冲突报告，仍不得回退当前活动方案。

#### Scenario: Agent starts a source debug session

- **WHEN** Agent以`invoke_extension_tool(tool_name="start_debugging", arguments={fileFullPath, workingDirectory, ...})`发起调用
- **THEN** dispatcher用产生该调用的sealed目录binding定位目标，再按当前thread和工作区解析文件/方案，创建仅属于该thread的调试运行时并返回真实状态

#### Scenario: 显式方案、活动方案与无方案启动顺序

- **WHEN** 当前thread存在活动方案，Agent分别以属于该thread的`debugConfigurationId`、不带ID、以及无任何方案时的路径参数通过固定信封启动
- **THEN** 前三种状态分别选择指定方案、当前活动方案及按安全路径创建的当前thread新方案；指定ID在其它thread或不存在时返回明确错误且不回退，不改变其它thread运行时

#### Scenario: 已保存方案拒绝冲突启动参数

- **WHEN** Agent选中显式或活动方案，但必填`fileFullPath`、`workingDirectory`与方案解析出的实际路径不同，或显式`configurationName`与该方案解析出的profile不同
- **THEN** 返回`debug_launch_parameter_conflict`及冲突字段，不忽略已知参数、不激活方案、不停止已有调试进程且不创建新进程；语义相同的Workspace相对/绝对路径规范化后允许启动，显式ID不存在仍优先返回`debug_configuration_not_found`

#### Scenario: 第N次命中只影响当前thread

- **WHEN** Agent通过固定信封为当前thread的普通断点或日志点指定`hitCondition=N`
- **THEN** 后端按本次目标进程对该断点的第N次到达计数，只有该次到达且可选条件同时为真时暂停或记录日志；另一个thread的相同路径/行号不共享命中计数，进程重启后重新计数

#### Scenario: Invalid tool arguments are rejected

- **WHEN** Agent传入缺少必填字段、非正整数行号/命中次数、空变量数组、不支持的scope、未知目标参数或多余外层字段
- **THEN** 目标调用失败并返回与原tool_call_id配对的明确参数错误，不启动或改变任何thread的调试运行时

### Requirement: 调试运行时和持久资源只属于发起调用的SessionThread

系统 SHALL以权威catalog校验的`(session_id, thread_id)`绑定每个Node调试进程、动态Inspector端口、活动方案、断点、状态和动作审计。Agent调用只从受信`ThreadRuntimeBinding`取得owner，模型不能以参数选择其它thread、Inspector端口/WebSocket、VS Code或DAP目标。同Session的main与child是不同调试owner；普通Session产品API只解析其唯一main thread，显式child产品API须验证child属于该Session并最终调用同一thread-qualified服务端口。调试存储路径必须经thread catalog解析到真实thread node下的`debug/node/`，归属manifest、运行状态和动作审计DTO须显式携带并校验`session_id`和`thread_id`；可移植方案正文不得嵌入owner，由受检thread目录/归属manifest建立关联，不得拼接session路径或扫描日期目录猜测owner。

调试start/stop/restart及独立Web/API mutation MUST在Session生命周期准入下由既有execution lease或专用debug operation lease覆盖，防止catalog删除与进程创建交错；debug owner MUST向唯一外部资源lease账本提供typed`node_debug_process` identity、真实进程状态和恢复引用。该跨Turn占用由debug owner以每次启动唯一`process_instance_id`持有，独立于本次tool/Web operation lease；在spawn前必须durably登记绑定精确thread、lifecycle generation、launch preimage和一次性nonce的`launch_pending` claim。spawn后只有核对nonce、OS进程起始身份与Inspector握手，才可把PID/端口登记为该实例属性并转入运行态；不能以PID、端口或内存句柄单独识别旧进程。`starting|running|paused|stopping`及尚未核实的`launch_pending`均是该thread的idle blocker，30分钟idle计时只在进程经owner核实进入`idle|exited|failed`、相关lease结清且其它blocker为空之后开始/恢复；不能由scope close、仅凭内存字典缺项或TTL宣称stopped。stop失败、进程状态不可确认或恢复未完成时保留`reconcile_required` blocker，不能进入cold。正常终态后debug owner释放该blocker，residency manager只消费已核实状态，不推断进程业务规则。

#### Scenario: Actions use the exact current thread runtime

- **WHEN** 同一Session的main与child各有一个调试进程，child Agent调用`continue_execution`、单步、断点或检查目标
- **THEN** 系统只控制child thread的进程和状态；main的暂停点、断点、活动方案、端口、动作审计及ToolSet绑定均不变

#### Scenario: No active debug runtime for this thread

- **WHEN** 当前thread未启动调试进程，即使同Session其它thread正在调试，Agent仍调用需要活动上下文的目标
- **THEN** 该调用返回当前thread无活动调试进程的明确错误，不借用其它thread状态或虚报成功

#### Scenario: Starting again replaces only the current thread runtime

- **WHEN** Agent在当前thread已有运行中调试进程时再次调用`start_debugging`
- **THEN** debug owner先真实停止/清理该thread旧进程，再创建新进程；同Session其它thread和其它Session均不受影响

#### Scenario: Session与child产品入口解析同一权威owner

- **WHEN** UI/API使用Session调试入口或明确的child thread调试入口读写状态、方案与断点
- **THEN** Session入口只解析main，child入口验证parent Session并绑定精确thread；响应/SSE/trace返回实际product thread identity供展示与审计，不把客户端给出的旧session缓存或模型参数当owner证明

#### Scenario: cold runtime与thread删除分开处理

- **WHEN** child存在`starting|running|paused|stopping`调试进程，fake clock越过30分钟idle阈值；随后进程经owner核实终止，或该child被正式删除
- **THEN** 活动调试进程期间thread仍resident，`blocking_reasons`标示脱敏debug blocker；核实终态和lease结清后才重新起算30分钟；thread删除则经lifecycle fence排空有效调用后由debug owner核实并停止本thread进程，释放其资源，旧generation callback不得写入新owner或其它thread

#### Scenario: backend重启或停止失败不得漏掉进程

- **WHEN** backend重启后持久lease仍显示调试进程占用，或stop失败/实际进程状态不可达
- **THEN** debug owner按精确thread和恢复引用核实进程/端口并恢复状态或返回`reconcile_required`，residency保持阻断；不从`LifetimeScope`关闭或服务内存消失推断停止成功

#### Scenario: spawn与持久登记之间崩溃或PID复用

- **WHEN** debug owner已持久登记`launch_pending`但在spawn前、spawn后尚未登记进程属性、或终态lease结清前崩溃；重启时相同PID/端口可能属于新进程
- **THEN** 恢复只按该thread的`process_instance_id`、nonce、OS进程起始身份与启动preimage核实原实例：证实不存在才结清，证实仍运行才接管/按owner策略停止，不能证实则保留`reconcile_required`及idle/delete blocker并提供诊断；不得认领、停止或回调写入仅PID/端口碰巧相同的其它进程

#### Scenario: 旧Session调试文件定点迁移

- **WHEN** itemized的显式maintenance迁移旧Session节点`debug/node/`数据
- **THEN** 静态接线的调试domain迁移步骤只按冻结的Session→main thread映射和已登记文件manifest在共享maintenance gate内定点迁入main thread节点，保留方案identity/revision/lineage和正文bytes/hash并校验完整性；活Inspector连接不复制，普通runtime不提供旧目录reader/alias、不扫盘补缺，失败保留旧原件及可恢复迁移状态而不半发布

#### Scenario: 可移植方案复制到目标thread

- **WHEN** 同Workspace公开fork按其模式复制可移植调试方案到target thread，目标本地方案ID可能与source不同
- **THEN** debug owner冻结目标有效Workspace debug配置revision/hash，对被选方案的入口、工作目录、每个断点路径、有效profile及adapter/runtime逐项校验并在发布前复核配置revision；缺失、漂移或不兼容使整个fork在target发布前失败，不跳过方案、不改用默认profile；target使用本地方案ID与source lineage，但不自动激活任何方案、不复制进程/端口/动作审计，source进程继续独立运行

#### Scenario: 跨Workspace方案复制不冒充公开fork

- **WHEN** 调用方试图通过公开fork、Gateway federation grant或migration-only child copy把调试方案复制到另一Workspace
- **THEN** 本次变更明确拒绝该操作；无owner的可移植正文仅是未来独立授权export/import协议的格式基础，不代表当前已提供跨Workspace方案复制或绕过目标Workspace校验的能力

### Requirement: Execution controls report authoritative debug state

系统 SHALL 支持 `stop_debugging`、`restart_debugging`、`continue_execution`、`pause_execution`、`step_over`、`step_into` 和 `step_out`。每个成功的控制动作 SHALL 返回后端确认后的完整调试状态，而不是只返回本地预期状态。

调试状态至少 SHALL能表达：`idle`、`starting`、`running`、`paused`、`stopping`、`exited`和`failed`；`stopping`是尚未核实进程真正终止的状态，不能提前解除idle blocker。状态在适用时包含目标脚本、进程信息、暂停原因、调用栈、断点、输出、最近一次求值和动作记录。

#### Scenario: Agent continues from a breakpoint

- **WHEN** 当前thread的调试进程暂停且Agent调用`continue_execution`
- **THEN** 程序继续执行，工具返回真实的 running、paused、exited 或 failed 状态及其最新快照

#### Scenario: Agent pauses a running program

- **WHEN** 当前thread的调试进程正在运行且Agent调用`pause_execution`
- **THEN** 系统请求目标程序暂停，并在成功暂停后返回当前调用栈和暂停位置

#### Scenario: Agent steps from a paused frame

- **WHEN** 当前thread的调试进程暂停且Agent调用任一单步目标
- **THEN** 系统执行对应单步动作，并返回新的暂停位置、调用栈和变量快照

#### Scenario: Control is invalid for the current state

- **WHEN** Agent 在已退出、失败或没有暂停上下文时调用不适用的控制动作
- **THEN** 工具返回明确错误和当前真实状态，不伪造单步或继续成功

### Requirement: Breakpoint and logpoint operations are observable

系统 SHALL支持普通断点、条件断点、命中次数断点和logpoint的新增、移除、列举与全部清理。断点状态 SHALL至少包含路径、请求行号、实际绑定行号（如果适用）、是否已验证以及条件、命中次数或日志表达式信息。

Node Inspector当前通过条件表达式实现不暂停logpoint，命中时写入可识别输出并返回false；SHALL保留现有能力，插值/条件求值失败需显式诊断，不得静默将logpoint当作普通暂停断点。仅其它真正不支持该能力的adapter可以返回明确`UNSUPPORTED_DEBUG_FEATURE`。

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
- **THEN** Node adapter安装条件表达式日志点，命中时在当前thread输出日志且不中断执行；无该能力的其它adapter才返回明确的不支持错误，不得把日志点降级为暂停断点

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

每个调试动作，包括Agent或Web/API发起的start、stop、continue、pause、step、断点变更、方案管理、变量读取和表达式求值，SHALL进入当前thread的动作时间线，并至少包含动作/目标名、`session_id`、`thread_id`、`actor_kind`、来源身份、时间和真实结果；Agent动作必须带原`tool_call_id`与sealed extension target binding，Web/API动作必须带受信principal/request_id，不伪造tool_call_id。history/Web可按受权thread查看，模型结果不得暴露可用于跨thread控制的内部句柄；变量、表达式和Logpoint输出沿用既有模型结果脱敏合同。

#### Scenario: Successful tool result replaces stale state

- **WHEN** 调试动作成功
- **THEN** Agent 和 Web 消费到包含完整最新状态的结果，不能只收到局部字段补丁

#### Scenario: Failed tool result is diagnosable

- **WHEN** 底层调试器、路径解析或表达式求值失败
- **THEN** 工具返回失败 code、明确原因和当前可获得的真实状态，不能吞掉异常
