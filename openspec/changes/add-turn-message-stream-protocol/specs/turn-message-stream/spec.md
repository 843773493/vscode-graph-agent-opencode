## Purpose

为一次 Turn 提供可排序、可恢复、可细粒度消费的模型消息流，使 reasoning、文本、工具和中断状态能够在上游产生后及时展示，并在断线或后端重启后恢复为权威状态。

## Canonical vocabulary

- `Turn` 是一次用户请求的业务回合；`Job` 是执行该 Turn 的后端运行记录；`TurnStream` 是该 Turn 的逻辑消息流，不随 retry/restart 更换，也不是 SSE 连接。
- 公共事件信封类型为 `MessageStreamEvent`；`TurnStreamEvent` 不作为 v1 公共类型名。
- `MessageBlock`/`BlockDelta` 是协议语义；旧历史或前端的 `ResponsePart` 只是投影，不是 v1 事件实体。
- `status` 表示生命周期，`outcome` 表示结果，`completion_reason` 表示关闭原因。未知工具结果统一表示为 `status=completed, outcome=outcome_unknown`。
- `active_state.kind` 的联合结构固定为：`model_output(phase=reasoning|text)`、`tool_call(phase=accumulating|stopping)`、`tool_execution(phase=running|stopping)`、`activity`、`interrupting`、`terminal`。
- snapshot 的 `snapshot_seq` 是整个 TurnStream 的投影高水位；实体投影使用 `started_seq`、`last_event_seq` 和可选 `completed_seq` 表达生命周期边界，`started_at`、`updated_at`、`completed_at` 仅用于展示，不能取代 `event_seq` 排序。

## Requirement directory

正文按以下责任域组织；同一条规则只在所属 Requirement 中定义，跨域场景通过字段和事件名引用：

1. 协议身份与顺序：消息流信封、`turn_stream_id`、`event_seq`。
2. 模型与工具事实：carrier、block、ModelCall、ToolCall、ToolExecution。
3. 数据生产与提交：provider raw delta、规范化、checkpoint、live fanout。
4. 运行时控制：Turn scope、CancellationSignal、ResourceManager、AgentControlInbox。
5. 恢复与重连：崩溃收敛、snapshot、replay、hydration。
6. 扩展与终态：Activity Handler、stream terminal gate。
7. 前端投影：reducer、renderer、连接状态和历史 hydration。

## ADDED Requirements

### Requirement: [Protocol] Message stream events have stable ordering and scope

系统 MUST 为每个 Turn 建立独立的消息流，并为每个业务事件提供 `session_id`、`turn_id`、可选的 `job_id`、`turn_stream_id`、`event_id` 和单调递增的 `event_seq`。事件类型至少覆盖 `stream.opened`、`stream.snapshot`、`model.*`、`block.*`、`tool_call.*`、`tool.*`、`activity.*`、`interrupt.requested`、`interrupt.rejected`、`stream.completed`、`stream.interrupted` 和 `stream.failed`。`stream.snapshot` 是由 checkpoint 生成的控制帧，其 `event_seq` 表示投影高水位而不是新的业务序号，可以重复发送同一个序号。`turn_stream_id` MUST 标识逻辑消息流而不是传输连接，业务事件顺序 MUST 以同一消息流内的 `event_seq` 为准。

#### Scenario: A stream starts with a stable identity

- **WHEN** 一个 Turn 开始产生响应
- **THEN** 系统先创建可重连的 `turn_stream_id`，并发布带 `event_seq` 的 `stream.opened`，后续事件都引用同一消息流标识

#### Scenario: Duplicate events do not change the projection

- **WHEN** 客户端因重连再次收到已经处理过的 `event_id` 或不大于已确认游标的 `event_seq`
- **THEN** 客户端忽略重复事件，不重复追加 block、工具调用或文本

#### Scenario: Events from another stream cannot be applied

- **WHEN** 客户端收到 `turn_stream_id` 不属于当前消息流的事件
- **THEN** 客户端拒绝应用该事件，并显式记录协议关联错误

#### Scenario: Job retry keeps the TurnStream

- **WHEN** 同一 Turn 的 Job 因模型校验、进程恢复或执行重试产生新的 Job/attempt
- **THEN** 继续使用同一个 `turn_stream_id`，仅创建新的 `job_id` 或 `model_call_id` 关联；不得通过新建消息流隐藏同一 Turn 的连续性

### Requirement: [Protocol] Lifecycle, outcome and completion fields have distinct boundaries

系统 MUST 使用 `status` 表达实体生命周期，使用 `outcome` 表达结果/原因，使用 `completion_reason` 表达闭合原因。`status` 不得承载 `outcome_unknown`、`user_interrupt` 等结果值；`outcome` 也不得替代实体仍处于 `running`、`stopping` 或 `completed` 的生命周期状态。对已经启动但结果不可确认的 ToolExecution，统一使用 `status=completed, outcome=outcome_unknown`，并同时提供可用的 `completion_reason`。

#### Scenario: An unknown tool result closes the execution fact

- **WHEN** `tool.started` 已提交，但工具返回、取消或恢复流程无法确认真实结果
- **THEN** 系统提交 `tool.completed(status=completed, outcome=outcome_unknown, completion_reason=...)`，前端不显示成功，也不继续显示 running

#### Scenario: A partial tool call is not an unknown execution

- **WHEN** ToolCall 参数未完成或已完成但从未产生 `tool.started`
- **THEN** 系统只提交 `tool_call.completed(status=incomplete|cancelled, completion_reason=...)`，不创建 `ToolExecution`，不使用 `outcome_unknown` 表示该调用

### Requirement: [Recovery] Snapshots preserve entity lifecycle ordering

系统 MUST 在 `stream.snapshot` 的 `model_calls[]`、`blocks[]`、`tool_calls[]`、`tool_executions[]` 和 `activities[]` 投影中保留每个已出现实体的 `started_seq` 与 `last_event_seq`，实体闭合后 MUST 保留 `completed_seq`。这些序号 MUST 来自同一 `turn_stream_id` 的已持久化 `event_seq`；可选的 `started_at`、`updated_at`、`completed_at` 只用于展示，不能作为权威排序依据。数组顺序、`updated_at` 或 snapshot 传输时间不得被客户端当作跨实体事件顺序。

#### Scenario: Snapshot exposes lifecycle boundaries

- **WHEN** 一个 ModelCall、MessageBlock、ToolExecution 或 Activity 已经产生至少一个生命周期事件，服务端生成 snapshot
- **THEN** 对应实体包含 `started_seq`、`last_event_seq`，如果已经闭合则包含 `completed_seq`；这些序号均不大于 snapshot 的 `snapshot_seq`

#### Scenario: Consecutive compactions remain separate in one snapshot

- **WHEN** 同一个 Turn 内先后发生两次 `context.compaction`，且两次 Activity 使用不同的 `activity_id`
- **THEN** snapshot 的 `activities[]` 同时包含两个 Activity，并分别保留各自的 `started_seq`、`last_event_seq` 和完成状态；后一个 Activity 不得覆盖或合并前一个 Activity

#### Scenario: Compaction does not create a new TurnStream

- **WHEN** AgentLoop 在同一个用户 Turn 的两个 ModelCall 之间自动执行上下文压缩
- **THEN** 压缩以该 TurnStream 内的 `context.compaction` Activity 表示；`turn_id` 与 `turn_stream_id` 保持不变，压缩后的真实 Provider 请求使用新的 `model_call_id`，不得因为上下文内容发生变化而创建新的 Turn、Job 或消息流

#### Scenario: Snapshot taken during the second compaction preserves the first result

- **WHEN** 第一次上下文压缩已完成，第二次上下文压缩已启动但尚未完成，服务端在第二次压缩期间生成 snapshot
- **THEN** snapshot 将第一个 Activity 表示为 `status=completed`，将第二个 Activity 表示为 `status=running|waiting|stopping`，并令 `active_state.activity_id` 指向第二个 Activity；两个 Activity 的生命周期序号关系可被客户端验证

#### Scenario: A compacted snapshot does not invent omitted update events

- **WHEN** 客户端请求的 replay 游标早于事件保留范围，只能获得包含多个 Activity 的 snapshot
- **THEN** 客户端依据实体生命周期序号恢复已知的先后和当前状态；snapshot 不伪造已经被压缩掉的 `activity.updated` 或 `block.delta`，需要中间更新时继续通过 event log replay 获取

#### Scenario: Entity arrays are not the global timeline

- **WHEN** snapshot 中的 `activities[]`、`model_calls[]` 和 `blocks[]` 以各自的投影顺序返回
- **THEN** 客户端不得以数组位置或墙上时间推断 ModelCall、压缩 Activity 和后续 Block 的跨实体顺序；需要严格顺序时使用生命周期序号，完整事件顺序使用同一 TurnStream 的 `event_seq`

### Requirement: [Model] Model output is represented as ordered message blocks and deltas

系统 MUST 将模型消息中的每个有序 carrier 表示为独立的 `MessageBlock`，并通过 `block.started`、一个或多个 `block.delta` 和 `block.completed` 表达其生命周期。每个 block MUST 具备稳定的 `block_id`、`block_index` 和 carrier 类型；同一个 block 内的 delta MUST 保持顺序。

#### Scenario: Reasoning becomes visible on its first semantic delta

- **WHEN** 上游产生第一个可识别的 reasoning carrier 增量
- **THEN** 系统在不等待完整 AIMessage 的情况下提交对应的 block 生命周期和第一个 `block.delta`

#### Scenario: Different carriers preserve their type and order

- **WHEN** 一个模型响应依次产生 `reasoning_content`、`thinking`、`reasoning_items`、`redacted_thinking` 或 `text` carrier
- **THEN** 消息流保留每个 carrier 的原始类型、顺序和 block 边界，不能把它们合并为一个无类型字符串

#### Scenario: Structured reasoning is not flattened

- **WHEN** `reasoning_items` 含有 item id、状态、reasoning text 或 summary text
- **THEN** `block.delta` 以结构化追加或更新表达这些字段，客户端可以隐藏敏感字段但不能把结构丢失为普通文本

#### Scenario: Redacted reasoning is not exposed as readable text

- **WHEN** 上游产生 `redacted_thinking`
- **THEN** 系统只发布其不可见或已脱敏状态，不把原始脱敏载荷当作前端可读 reasoning 展示

### Requirement: [Model/Tool] Partial blocks and tool calls have explicit interruption closure

系统在进程仍可提交时 MUST 为每个已创建但未正常结束的 `MessageBlock` 提交 `block.completed`，并携带 `completion_reason` 与 `partial`；进程直接崩溃时，恢复流程 MUST 在 snapshot 中补出等价闭合事实。系统 MUST 为已创建但未完成的 `tool_call` 提交独立的 `tool_call.completed` 或在恢复 snapshot 中补出等价事实；不能只停止后续 delta 或让客户端从一个长期 running 的投影猜测它是否被中断。

#### Scenario: A reasoning or text block is interrupted

- **WHEN** reasoning 或 text block 已提交至少一个 delta，随后 `interrupt.requested` 被线性化
- **THEN** 系统拒绝新的 block.delta，并提交 `block.completed(completion_reason=user_interrupt, partial=true)`；已提交内容保留为部分结果，不得标记为正常上游完成

#### Scenario: A partial tool call is interrupted before execution

- **WHEN** tool call 的名称或参数仍在分片接收，尚未产生 `tool.started`，随后用户中断 Turn
- **THEN** 系统提交 `tool_call.completed(status=incomplete, completion_reason=user_interrupt, arguments_complete=false)`，不产生 `tool.started`，前端将其显示为已中断的未完成调用而不是运行中工具

#### Scenario: A complete tool call is cancelled before execution

- **WHEN** tool call 参数已经完整，但 AgentLoop 尚未启动工具执行，随后用户中断 Turn
- **THEN** 系统提交 `tool_call.completed(status=cancelled, completion_reason=user_interrupt)`，不启动工具，也不把它显示为工具成功或未知执行结果

#### Scenario: Provider content carrier changes during a model call

- **WHEN** Provider 从 reasoning carrier 切换到 text 或其它 content carrier
- **THEN** 规范化器先闭合前一个 block，再创建新的 content block；空 metadata/usage/finish chunk 不产生空的可见 block.delta

#### Scenario: Provider switches from content to tool calls

- **WHEN** Provider 从 reasoning/text content carrier 切换到 `tool_calls[]`
- **THEN** 规范化器先闭合当前 content block，再开始 `tool_call.delta` 生命周期；tool call 不被伪装成 MessageBlock

### Requirement: [Model] Model calls and retries are explicit

系统 MUST 为每一次真实的上游模型请求分配独立的 `model_call_id` 和 attempt 序号，并通过模型生命周期事件表达开始、完成、失败和重试。首个语义 delta 已提交后，系统 MUST NOT 将同一个 ModelCall 静默重试为另一个请求。

#### Scenario: A model call emits lifecycle boundaries

- **WHEN** 一次上游模型请求开始、返回、失败或触发业务重试
- **THEN** 消息流分别表达 `model.started`、`model.completed`、`model.failed` 或 `model.retrying`，并携带对应的 ModelCall 标识和结果

#### Scenario: User interruption closes the current model attempt

- **WHEN** `interrupt.requested` 已持久化，Provider stream 被关闭且当前 ModelCall 尚未正常完成
- **THEN** 系统提交 `model.failed(outcome=user_interrupt, retryable=false)`；它不表示完整模型结果，也不得触发同一 ModelCall 的静默重试

#### Scenario: Validation retry does not masquerade as completion

- **WHEN** ModelCall 已产生文本但 Turn 的业务校验发现必须重新请求模型
- **THEN** 该文本被标记为中间或已取代结果，系统在最终校验通过前不得发布 `stream.completed`

#### Scenario: A completed model call is not emitted twice before retry

- **WHEN** 当前 ModelCall 已发布 `model.completed(outcome=validation_failed)`，随后 AgentLoop 进入下一 attempt
- **THEN** 系统不重复发布该 ModelCall 的 `model.completed`，而是保留原 attempt 并发布 `model.retrying` 与新的 `model.started`

#### Scenario: Crash occurs before retry decision

- **WHEN** 后端在 ModelCall 完成与下一次重试决策之间崩溃
- **THEN** 重连 snapshot 返回当前 ModelCall、attempt 和 validating/retrying 状态，不能把上一轮文本报告为 Turn 已完成

### Requirement: [Runtime] Turn execution control is scoped and composable

系统 MUST 为每个 Turn 建立独立的运行时执行 scope，并在同一 Turn 的多个 ModelCall、Provider stream、ToolCall 和其它长耗时操作之间传播其取消信号。执行 scope MUST 与 `turn_stream_id` 关联但不得进入 `message.v1` payload；取消信号 MUST 至少表达取消原因并支持取消 hook，ModelCall 和 ToolCall MUST 可以派生独立的 child scope。执行 scope 只拥有临时操作和持久资源 lease，不拥有 terminal、browser、MCP 连接或开发服务等持久资源的生命周期。

#### Scenario: A Turn owns one execution scope

- **WHEN** 一个 Turn 开始执行并先后产生多个 ModelCall 或 ToolCall
- **THEN** 这些操作共享该 Turn 的父执行 scope；另一个 Turn 使用独立 scope，取消一个 Turn 不得取消另一个 Turn

#### Scenario: Cancellation is passed only to long-running boundaries

- **WHEN** 系统调用 Provider、工具或其它可能阻塞的外部操作
- **THEN** 调用边界可以接收该 Turn 的执行 scope 或取消信号；纯数据转换、事件序列化和普通查询不因该机制被强制携带运行时 token

#### Scenario: Parent cancellation reaches child operations

- **WHEN** 用户请求中断一个仍在运行的 Turn
- **THEN** 该 Turn 的取消信号级联到当前 ModelCall、Provider stream 和运行中的 ToolCall，子操作收到明确的用户中断原因并进入各自的清理路径

#### Scenario: A child timeout does not imply a Turn interrupt

- **WHEN** 单个 ModelCall 或 ToolCall 达到其局部 deadline，而父 Turn 没有收到中断请求
- **THEN** 系统结束该 child scope 并向 AgentLoop 提供超时事实，由 AgentLoop 决定重试、失败或继续；不得伪造用户 `interrupt.requested` 或直接把整个 Turn 标记为 `stream.interrupted`

#### Scenario: Cancellation releases upstream and tool resources

- **WHEN** Turn 或 child scope 被取消且 Provider/工具已经持有网络、stream、子进程或浏览器资源
- **THEN** 取消 hook 触发对应的 abort/close 清理并释放当前操作 lease；Provider 不再继续向消息流提交新的 delta，工具结果无法确认时保留 `outcome_unknown`，持久资源本身不因 Turn 取消而默认销毁

#### Scenario: Runtime cancellation is not the recovery authority

- **WHEN** 前端断开连接或后端进程在 Turn 执行期间崩溃
- **THEN** 前端断开不得自动取消仍在运行的 Turn；后端重启后不得依赖已经消失的内存取消信号，而必须根据持久化事件和 checkpoint 恢复中断、失败或 `execution_lost` 状态

### Requirement: [Runtime] Persistent resources have independent lifecycle and operation leases

系统 MUST 由独立的 `ResourceManager` 管理跨 Turn 的 terminal、browser context、MCP connection、development server 等持久资源。TurnExecutionScope 只能持有资源的 operation lease 和当前操作 scope；取消、超时或前端断线 MUST NOT 默认销毁持久资源。资源销毁 MUST 通过 `cleanup_policy` 或经过授权的显式 stop 命令完成。

#### Scenario: Turn interruption stops an operation but keeps a durable resource

- **WHEN** Turn 正在持有一个 terminal server 或 browser context 的 lease，并在其中执行命令或导航时收到用户中断
- **THEN** 系统中止当前操作、释放 lease，并保留资源的独立生命周期状态；`stream.interrupted` 不表示资源已销毁或操作副作用已回滚

#### Scenario: Explicit resource stop is separate from Turn cancellation

- **WHEN** 用户或工作区控制面请求停止一个持久资源
- **THEN** `ResourceManager` 独立校验 `resource_id`、lease/所有权和资源状态后执行 stop；不能通过调用 Turn 的 `CancellationSignal` 代替资源 stop，也不能因为 SSE 断开隐式 stop

#### Scenario: Resource operation result is reconciled by lease

- **WHEN** 一个资源操作的异步结果到达，且对应 Turn 可能已经取消、崩溃或释放 lease
- **THEN** 系统按 `resource_id`、`lease_id` 和 `operation_id` 校验结果；过期结果不能重新启动工具或改变已终态 Turn，未知结果保留为资源操作未知事实

#### Scenario: Crash recovery does not blindly kill durable resources

- **WHEN** 后端在持久资源操作或 Turn scope 存活期间崩溃
- **THEN** 恢复流程由 `ResourceManager` reconcile 资源和 lease，区分 `recovered`、`orphaned` 与未知操作；不得因为 Turn 崩溃自动杀掉所有持久资源或自动重放未知操作

### Requirement: [Runtime] AgentLoop receives ordered control messages separately from cancellation

系统 MUST 为每个 Turn 提供类型化、按序、可幂等去重的 `AgentControlInbox`，用于接收 interrupt、steer、approval.result、resume 和资源操作结果等重复控制输入。`CancellationSignal` MUST 只表达一次性取消广播；`AgentControlInbox` MUST 不承载 provider raw chunk、AIMessageChunk 或消息流输出事件。所有 provider delta、工具结果、控制命令和取消信号 MUST 由同一个 AgentLoop 协调器按线性化顺序处理。控制命令的 accepted/consumed/rejected 状态属于控制 API/控制日志；只有 interrupt 的用户可见请求/拒绝事实投影为 `interrupt.requested`/`interrupt.rejected`。Inbox 本身 MUST NOT 作为恢复权威；可接受的控制意图必须先写入可恢复的 Turn 控制检查点/控制日志。

#### Scenario: Interrupt has one linearized cancellation path

- **WHEN** 用户重复提交同一个 interrupt 命令或多个并发 interrupt 命令
- **THEN** 协调器按命令幂等键和消息流顺序只提交一次 `interrupt.requested`，然后只触发一次 Turn scope cancellation；不得由 inbox 和其它 task cancellation 路径各自重复执行语义打断

#### Scenario: Late controls cannot revive an interrupted stream

- **WHEN** `interrupt.requested` 已提交或 stream 已进入 `interrupting`/terminal，随后到达 steer、resume、approval.result 或业务资源操作结果
- **THEN** 系统返回明确的控制拒绝或清理确认，不能产生新的模型/block/tool 事实，不能重新打开 stream，也不能重新启动已取消的 ToolCall

#### Scenario: Approval is bound to a pending ToolCall

- **WHEN** approval.result 到达 AgentLoop
- **THEN** 只有仍在等待该 approval 的指定 ToolCall 可以消费它；Turn 取消、ToolCall 超时或后端恢复为未知状态后，迟到审批结果必须被拒绝，不得把未知副作用自动重放为成功

#### Scenario: Steer and resume do not rewrite committed deltas

- **WHEN** AgentLoop 在允许控制输入的状态收到 steer 或 resume
- **THEN** 控制只影响后续模型输入、等待状态或恢复策略，不修改已经持久化的 block.delta，不覆盖已有 event_seq，也不把历史中间 ModelCall 改写成最终答案

#### Scenario: Host shutdown retains an external cancellation reason

- **WHEN** 应用或工作区关闭导致外层生命周期信号级联到活动 Turn
- **THEN** AgentLoop 释放当前操作并记录外部取消/执行丢失事实，但不伪造用户 `interrupt.requested` 或 `stream.interrupted`

#### Scenario: A crash between control acceptance and consumption is recoverable

- **WHEN** 控制命令已经被接受但 AgentLoop 尚未消费，后端在 inbox 入队、消费或结果提交之间崩溃
- **THEN** 重启流程根据持久化的 command 状态和当前 checkpoint 重新校验该命令；只恢复仍安全且仍适用的等待/控制意图，不能依赖内存 inbox，也不能自动重放可能产生副作用的工具或资源操作

### Requirement: [Tool] Tool calls and tool executions have separate facts

系统 MUST 区分模型声明的 `tool_calls[]`、工具开始执行和工具结果，并为工具执行表达生命周期 `status=completed|failed` 与结果 `outcome=success|outcome_unknown`。`tool.started` 已提交而结果无法确认时，系统 MUST 在进程仍可提交时发布 `tool.completed(status=completed, outcome=outcome_unknown)`；如果进程直接崩溃，恢复流程 MUST 补出等价的权威事实。工具执行结果未知时，系统 MUST 保留该事实且默认不得自动重放工具。

#### Scenario: Tool call is separate from tool result

- **WHEN** 模型声明一个工具调用并且后端开始执行该工具
- **THEN** 消息流分别表达工具调用生命周期和 `tool.started`，工具返回后再表达 `tool.completed`

#### Scenario: Crash after tool start

- **WHEN** 后端已持久化工具开始执行但在工具结果持久化前崩溃
- **THEN** 恢复后的 snapshot 将该执行标记为 `status=completed, outcome=outcome_unknown`，且不会自动再次执行可能产生副作用的工具

#### Scenario: Interrupt does not hide an unknown tool result

- **WHEN** 用户在工具执行期间请求中断且工具结果无法确认
- **THEN** snapshot 和终态保留 `status=completed, outcome=outcome_unknown`，不能仅以 `stream.interrupted` 覆盖工具事实

#### Scenario: Live interruption publishes an unknown tool result

- **WHEN** `tool.started` 已提交，取消 hook 已停止当前操作，但工具没有返回可验证结果且后端仍可写入事件
- **THEN** 系统先提交 `tool.completed(status=completed, outcome=outcome_unknown)`；Provider 和工具确认停止时提交 `stream.interrupted`，停止过程有错误或无法确认时提交 `stream.failed(execution_lost, after_interrupt_requested=true)`，在线前端无需等待 snapshot 就能停止显示该工具为 running

#### Scenario: Reconnecting preserves tool call arguments

- **WHEN** 客户端通过 snapshot 恢复一个已经声明工具调用、但尚未完成工具执行的消息流
- **THEN** snapshot 同时返回 `tool_calls[]` 与 `tool_executions[]`，客户端可以恢复工具名称和参数，而不把工具调用误报为工具结果

#### Scenario: Tool call fragments retain one provider identity

- **WHEN** provider 在多个 delta 中逐步发送同一个工具调用，后续 delta 缺少名称或只包含部分参数
- **THEN** 所有 `tool_call` 事件使用同一个 `tool_call_id`，规范化状态保留已经收到的非空工具名并合并参数；关联的 `tool.started` 和 `tool.completed` 使用独立的 `tool_execution_id`，但继续引用该 `tool_call_id`

#### Scenario: Custom tool entry and target tool remain distinguishable

- **WHEN** 模型通过 `invoke_custom_tool` 入口请求一个目标工具
- **THEN** `tool_calls[]` 保留 provider 入口名称及原始参数，`tool_executions[]` 可以展示解析后的目标工具名称，但前端通过 `tool_call_id` 同时恢复工具参数，不能因执行 run id 不同而显示空参数

### Requirement: [Ingestion] A normalized upstream delta is committed before live fanout

系统 MUST 在上游 raw chunk 与最终 `AIMessageChunk`/AIMessage 聚合之间产生规范化的模型 delta，并严格按 `raw chunk → NormalizedModelDelta → event/checkpoint commit → live fanout → AIMessageChunk → LangChain aggregation` 顺序处理。消息流和最终 AIMessage MUST 由同一份规范化结果派生；每个对外可见的 `block.delta` MUST 先与 checkpoint 一起持久化，再发送给在线订阅者；是否存在前端连接只影响 live fanout，不影响消息流提交。

#### Scenario: Backend task runs without a frontend subscriber

- **WHEN** 后台任务产生模型 delta 但当前没有前端连接
- **THEN** 系统仍持久化事件、block 投影和 stream 状态，不执行网络 fanout，稍后连接的客户端可以通过 replay 或 snapshot 获取状态

#### Scenario: A subscriber receives only committed events

- **WHEN** 一个规范化 delta 被处理
- **THEN** 只有在事件与对应 checkpoint 成功持久化后，在线订阅者才会收到它；持久化失败时订阅者不得看到该 event_seq

#### Scenario: A slow subscriber cannot stop model processing

- **WHEN** 在线订阅者的有界队列满了
- **THEN** 系统断开或暂停该订阅者并要求其使用 event_seq/snapshot 修复缺口，不能让模型上游无限等待该客户端

#### Scenario: Commit failure does not leak an uncommitted delta

- **WHEN** 规范化 delta 在 event/checkpoint 持久化完成前失败
- **THEN** 系统不得向在线订阅者发布该 event_seq，也不得继续把该未提交 delta 作为成功的 LangChain 流片段；AgentLoop 必须获得明确的失败

#### Scenario: Hook failure stops downstream aggregation

- **WHEN** raw chunk 规范化、TurnStream 提交或 checkpoint 持久化失败
- **THEN** provider 不得继续为该 chunk 生成 `AIMessageChunk` 或调用 LangChain 聚合；当前 ModelCall 进入明确的失败/恢复路径，不能由 `on_chat_model_stream` 补发第二份 delta

#### Scenario: A committed delta survives fanout failure

- **WHEN** event/checkpoint 已持久化，但 live fanout 前后发生断线、队列溢出或进程退出
- **THEN** 已提交的 event_seq 保持不变，客户端通过 replay 或 snapshot 获取该 delta，fanout 失败不能回滚消息流

#### Scenario: Every production provider uses the pre-aggregation hook

- **WHEN** LiteLLM Chat、LiteLLM Responses 或 Anthropic provider 收到 raw chunk
- **THEN** provider 在生成 `AIMessageChunk` 和调用 LangChain callback/聚合前，先将该 chunk 规范化并提交到同一个消息流 hook；生产 provider 不得把 `on_chat_model_stream` 重新作为实时消息流的回退来源

#### Scenario: A shared cancelable reader stops a pending provider read

- **WHEN** LiteLLM Chat、LiteLLM Responses 或 Anthropic SDK raw stream 正在等待 `anext()`，且该 Turn 的 `CancellationSignal` 被触发
- **THEN** 统一读取器主动取消 pending `anext()` 任务，调用并等待 Provider 的 `aclose()`/`close()`，再等待读取任务真正结束后抛出结构化取消；Provider 不得只依赖 `aclose()` 是否恰好唤醒 `anext()`，也不得为每个 Provider 复制一套取消竞速实现

#### Scenario: Current chunk processing is not cut in half

- **WHEN** raw chunk 已经被读取，规范化、消息流提交或 `AIMessageChunk` 转换期间收到取消
- **THEN** 当前消费任务完成该 chunk 的处理边界，不由读取器调用 `task.cancel()` 打断；writer 按 delta 与 `interrupt.requested` 的线性化顺序决定该 delta 是否生效，处理结束后的下一次读取停止上游

#### Scenario: Provider cleanup waits for the read task

- **WHEN** Provider 流在读取任务 pending 时收到用户中断、Provider 异常、消费者提前退出或外层 task cancellation
- **THEN** 统一读取器执行幂等 close，并等待读取任务结束后再退出；close 或读取任务异常不能静默吞掉，AgentLoop 获得明确的取消、Provider error 或 execution_lost 事实

#### Scenario: Empty provider chunks do not create phantom deltas

- **WHEN** Provider 发送只包含 usage、finish reason、role 或其它 metadata 的空 chunk
- **THEN** 规范化器只推进 ModelCall/block 的完成状态，不产生空的可见 `block.delta`、新的 block 或新的 tool call

#### Scenario: Missing reasoning item IDs remain stable

- **WHEN** `reasoning_items` 的 Provider chunk 没有稳定 item ID，但后续 chunk 继续更新同一 reasoning item
- **THEN** 规范化器在该 ModelCall 内生成并复用稳定的本地 item key，不能每个 chunk 创建一个新的 item 或丢失已有 patch

### Requirement: [Recovery] Stream state is recoverable across arbitrary crash boundaries

系统 MUST 将事件和能重建当前展示状态的 checkpoint 作为同一提交边界，并在后端重启后从持久化状态恢复。系统 MUST NOT 依赖后端在崩溃前发送 `backend.crashed` 事件，也 MUST NOT 先 fanout 后持久化。

#### Scenario: Crash before event commit

- **WHEN** raw delta 尚未完成规范化提交，或 event_seq 已分配但事件与 checkpoint 尚未成功持久化
- **THEN** 恢复状态不包含该 delta，也不伪造一个不存在的事件

#### Scenario: Crash after event commit but before fanout

- **WHEN** 事件与 checkpoint 已持久化但后端在 live fanout 前退出
- **THEN** 重连通过 replay 或 snapshot 补回该事件，且其 event_seq 不会改变

#### Scenario: Running state has no safe continuation proof

- **WHEN** 恢复扫描发现消息流仍处于运行中，但没有足够检查点证明模型或工具可以安全续接
- **THEN** 系统发布或在 snapshot 中表达 `stream.failed`，failure code 为 `execution_lost`，并将 `resumable` 设为 false

#### Scenario: Recovery does not silently replay side effects

- **WHEN** 崩溃发生在模型请求或工具执行边界
- **THEN** 系统恢复已知的 attempt 和工具事实，但默认不自动重放模型调用或可能有副作用的工具调用

#### Scenario: The interrupt gate rejects late model deltas

- **WHEN** `interrupt.requested` 已按顺序持久化，而 provider 随后才到达新的模型 delta 或 `stream.completed`
- **THEN** writer 拒绝新的 block/tool/model 产生事件和完成事件；已开始的 block/tool 只允许提交收尾事实，最终只能进入 `stream.interrupted` 或 `stream.failed`

#### Scenario: Repeated interrupt does not replace the first request

- **WHEN** stream 已处于 `interrupting`，用户再次点击中断
- **THEN** 系统返回 `interrupt.rejected(reason=already_interrupting)`，保留第一次的 `interrupt_request_id` 和请求状态

### Requirement: [Recovery] Reconnection uses cursor replay and authoritative snapshots

系统 MUST 支持客户端携带 `after_seq` 重连。服务端能够补齐游标之后的事件时 MUST 按序补发；无法保证完整事件区间时 MUST 返回完整 snapshot，并从 snapshot 序号之后继续发送新事件。客户端应用 snapshot 时 MUST 替换该消息流的完整 live projection。

消息流 JSON/SSE 边界中的 `event_seq` 与 `snapshot_seq` MUST 是非负整数，而不是 Protobuf JSON 默认的 int64 字符串表示；客户端可以直接用它们做连续性和游标运算。

`stream.snapshot` MUST 是该 `snapshot_seq` 对应的完整展示投影，至少包含 blocks、tool_calls、tool_executions、activities、current ModelCall/attempt、AgentLoop、active_state、interrupt、resource_refs、failure、recovery 和 resumable 状态；它 MUST 能独立恢复 partial block、incomplete/cancelled tool call、`outcome_unknown` ToolExecution、通用 Activity 和用户中断的 ModelCall，不得要求客户端保留此前 live event 才能解释当前状态。核心模型/工具状态的 `active_state.kind/phase` MUST 明确表示 model output 的 reasoning/text、tool_call、tool_execution、interrupting 或 terminal 阶段，并携带对应实体 ID；非核心耗时路径 MUST 使用 `active_state.kind=activity`，携带 `activity_id`、`activity_kind`、通用状态和可选 detail 引用，不得为每个扩展路径新增顶层 phase。`stream.snapshot` 是由 checkpoint 生成的协议控制帧，不是新的业务事实；其 envelope `event_seq` MUST 等于 `snapshot_seq`，不额外推进消息流序号。前端不得只按 `event_id` 去重相同序号的 snapshot。

#### Scenario: Reconnect with an available event range

- **WHEN** 客户端携带最后确认的 `after_seq` 重连且服务端保留该游标之后的事件
- **THEN** 服务端按 event_seq 连续补发缺失事件，客户端恢复后继续接收实时事件

#### Scenario: Reconnect after the event range is compacted

- **WHEN** 客户端请求的游标早于服务端可 replay 的最小序号
- **THEN** 服务端先返回包含 blocks、ModelCall、ToolExecution、AgentLoop、关联 resource_refs 和 interrupt 状态的 snapshot，再发送 snapshot_seq 之后的事件；resource_refs 只描述关联资源和操作/lease 状态，资源详情仍由 ResourceManager 提供

#### Scenario: Snapshot replaces stale disconnected content

- **WHEN** 客户端断线期间本地展示了尚未确认的 delta，随后收到权威 snapshot
- **THEN** 客户端以 snapshot 替换旧 projection，并保留连接状态和协议错误提示，不把临时内容误当作最终结果

#### Scenario: Snapshot contains complete interruption projection

- **WHEN** 客户端通过 `stream.snapshot` 恢复一个在 reasoning/text/tool_call/ToolExecution 或通用 Activity 任意阶段被中断的 Turn
- **THEN** snapshot 同时包含 partial blocks、incomplete/cancelled tool calls、`outcome_unknown` ToolExecution、Activity 的通用终态、ModelCall 的用户中断结果、interrupt 请求状态和 failure/recovery 状态；前端不依赖之前收到的 live delta 才能正确展示

#### Scenario: Snapshot identifies the reasoning phase

- **WHEN** snapshot 截取时当前 ModelCall 正在输出 reasoning carrier
- **THEN** `active_state.kind=model_output`、`active_state.phase=reasoning`，并携带当前 `block_id`、`carrier_type` 和 block 状态；`blocks[]` 同时保留此前已经完成的 block

#### Scenario: Snapshot identifies the text phase

- **WHEN** snapshot 截取时当前 ModelCall 正在输出 text carrier
- **THEN** `active_state.kind=model_output`、`active_state.phase=text`，并携带当前 text block 的 `block_id`；前端不会把此前的 reasoning block 误显示为仍在生成

#### Scenario: Snapshot identifies the tool-call phase

- **WHEN** snapshot 截取时模型正在累积 tool call 名称或参数，且工具尚未启动
- **THEN** `active_state.kind=tool_call`、`active_state.phase=accumulating`，并携带 `tool_call_id`；`tool_calls[]` 返回已确认的部分参数和 `arguments_complete`，`tool_executions[]` 不创建运行中的工具

#### Scenario: Snapshot identifies the tool-execution phase

- **WHEN** snapshot 截取时 `tool.started` 已提交且工具仍在执行
- **THEN** `active_state.kind=tool_execution`、`active_state.phase=running`，并携带 `tool_execution_id`、关联 lease 和执行状态；如果结果不可确认，active state 和 `tool_executions[]` 都必须显示 `status=completed, outcome=outcome_unknown`

#### Scenario: Snapshot identifies interruption or terminal phase

- **WHEN** snapshot 截取时 Turn 正在停止或已经进入终态
- **THEN** `active_state.kind` 分别为 `interrupting` 或 `terminal`，并携带 `last_kind`、可选 `last_phase`、中断/终态原因和对应实体状态；前端不通过数组扫描猜测当前阶段

#### Scenario: Snapshot sequence is the applied high-water mark

- **WHEN** 服务端发送 `stream.snapshot`
- **THEN** envelope 的 `event_seq` 等于 payload 的 `snapshot_seq`，snapshot 不额外消耗 event_seq；客户端应用后将游标推进到 snapshot_seq，后续只接受大于该序号的事件

#### Scenario: Reconnect registers before snapshot replay

- **WHEN** 客户端断线后携带 `after_seq` 重新连接
- **THEN** 服务端先建立该 `turn_stream_id` 的订阅，再读取 replay/snapshot；读取期间产生的事件不会落在订阅和 snapshot 之间的竞态窗口中

#### Scenario: Concurrent live events do not get lost around a snapshot

- **WHEN** snapshot 与 event_seq 大于或小于 snapshot_seq 的 live event 交错到达，或先收到 `snapshot_seq+2` 而尚未收到 `snapshot_seq+1`
- **THEN** 客户端暂存无法连续应用的事件，snapshot 完整替换 projection 后丢弃或去重不大于 snapshot_seq 的事件，并按序应用大于 snapshot_seq 的事件；存在中间缺口时继续 replay 或重新请求 snapshot，不产生伪缺口或重复 block

#### Scenario: A terminal snapshot ends frontend resubscription

- **WHEN** `stream.snapshot` 的 `stream_status` 是 completed、interrupted 或 failed
- **THEN** 前端应用完整 snapshot 后停止该 stream 的自动重连；open、running 或 interrupting snapshot 则保留订阅并继续接收后续事件

#### Scenario: Snapshot envelope fields remain outside the payload

- **WHEN** 服务端把包含 session、Turn 和 stream 定位字段的内部 checkpoint 编码为 `stream.snapshot`
- **THEN** 定位字段保留在事件信封中，`StreamSnapshot` payload 不重复承载未知的信封字段，JSON/Protobuf 两种边界都可以严格解码

### Requirement: [Extension] Non-core long-running paths use generic Activities with optional handlers

系统 MUST 为 Turn 内不属于核心 ModelCall、MessageBlock、ToolCall 或 ToolExecution 生命周期的耗时路径提供通用 `Activity` 投影。通用 Activity MUST 至少保留 `activity_id`、`kind`、可选父 Activity、公共归属 `scope_ref`、`running|waiting|stopping|completed|failed|unknown` 状态、结果/失败原因、取消与恢复能力、外部副作用策略、资源引用和最后更新时间；`scope_ref` 不得携带运行时 `TurnExecutionScope` 或 `CancellationSignal`。Activity 可以不保留阶段进度或 Provider 私有细节。Activity 的通用生命周期事件和状态变更 MUST 经过同一个 TurnStream 串行提交点，并与消息事件共享 event_seq、checkpoint、snapshot 和崩溃恢复边界。

系统 MUST 提供按 Activity `kind` 注册语义 Handler 的扩展机制。Handler 可以增加结构化 detail、细粒度事件、统一 `stream.snapshot` 内的专用 projection、取消/清理和恢复逻辑，但 MUST 不能创建新的 snapshot 类型、绕过消息流 writer、直接修改 checkpoint 或单独产生未持久化的前端事实。Handler 自身缺失、版本不兼容、detail 序列化失败或恢复逻辑失败时，系统 MUST 只回退到通用 Activity 投影，主 TurnStream 继续运行；但如果 Handler 失败导致底层副作用的结果或清理无法确认，Activity 必须进入 `outcome_unknown`/`execution_lost`，AgentLoop 不得据此提交虚假的 `stream.completed`。跨 Turn 的 Job 队列、Goal、后台任务、持久资源和 SSE 连接不强制成为 Turn Activity，分别由其所属生命周期管理并通过引用关联。

#### Scenario: An unregistered AgentLoop path uses the generic fallback

- **WHEN** AgentLoop 新增一条耗时路径但没有为其 `kind` 注册语义 Handler
- **THEN** 系统至少提交通用 Activity 的开始、状态更新和完成/失败事实，snapshot 可以显示“处理中/等待中/结果未知”等粗粒度状态，前端使用通用 Renderer，不把该路径误判为模型文本或运行中的工具

#### Scenario: A registered Handler adds fine-grained state

- **WHEN** 一个 Activity `kind` 注册了可用的语义 Handler
- **THEN** Handler 可以在通用 Activity 之外提交结构化 detail、细粒度进度、专用等待原因或恢复信息；这些扩展与通用投影在同一个 event/checkpoint 提交边界内保持一致

#### Scenario: Handler failure falls back without hiding safety facts

- **WHEN** Activity Handler 缺失、版本不兼容或恢复过程失败
- **THEN** 系统保留 Activity 的身份、当前状态、取消/恢复能力、资源引用和副作用边界，并将 detail 标记为不可用或未知；主 TurnStream 不因 detail 失败而停止，不得静默删除 Activity，也不得自动重放未知副作用

#### Scenario: Handler failure cannot authorize false completion

- **WHEN** Activity Handler 无法确认已启动的外部副作用是否停止、完成或清理
- **THEN** Activity 进入 `outcome_unknown` 或 `execution_lost`，AgentLoop 可以继续处理其它事实，但不得把该 Activity 当作成功并提交虚假的 `stream.completed`

#### Scenario: Crash recovery closes a generic Activity conservatively

- **WHEN** 后端在未注册 Activity 处于 running、waiting 或 stopping 状态时崩溃，且没有安全续接证明
- **THEN** 重启后的 snapshot 将其转为 `unknown` 或 `failed(execution_lost)`，`resumable=false`，保留摘要和资源引用；不会因为缺少专用 Handler 而伪造成功或自动重新执行

#### Scenario: An Activity with an external side effect is interrupted

- **WHEN** Activity 已经启动可能产生外部副作用的操作，随后 Turn 收到用户中断
- **THEN** 通用投影至少记录停止/未知结果和副作用策略；如果没有可验证的 Handler 清理或结果查询能力，Activity 必须进入 `outcome_unknown`，不能仅显示为普通 cancelled 或成功

### Requirement: [Runtime] Interrupt request and interruption fact are distinct

系统 MUST 将用户中断表达为幂等的 `interrupt.requested`，并将 AgentLoop 停止事实表达为 `stream.interrupted`。请求事件 MUST 与 delta、模型、工具和 stream 终态使用同一消息流顺序；请求持久化后才向该 Turn 的执行 scope 发送取消信号，取消当前操作并释放 operation lease。`stream.interrupted` 不得被解释为工具结果成功、持久资源已销毁或副作用已回滚。

#### Scenario: Interrupt is requested while the stream is active

- **WHEN** 用户对运行中的 Turn 发起中断
- **THEN** 系统先持久化 `interrupt.requested`，阻止其后的新 block/tool delta，再取消该 Turn 的执行 scope，闭合已有 partial block/tool_call，等待 Provider/工具确认停止并在 AgentLoop 停止事实确认后发布 `stream.interrupted`；未确认的工具结果仍以 `tool.completed(status=completed, outcome=outcome_unknown)` 或恢复等价事实标记，无法确认停止时改为 `stream.failed(execution_lost, after_interrupt_requested=true)`

#### Scenario: A delta races with an interrupt

- **WHEN** 一个 delta 与中断请求同时到达
- **THEN** event_seq 的线性化顺序决定结果；中断请求之后到达的迟到 delta 被拒绝，不得污染已进入中断闸门的 stream

#### Scenario: Interrupt is requested after terminal completion

- **WHEN** `stream.completed` 已经持久化后收到中断请求
- **THEN** 系统记录 `interrupt.rejected` 及 `already_terminal` 原因，不改变已完成的 stream

#### Scenario: Cancellation fails before the stop fact is committed

- **WHEN** 中断请求已记录但上游停止过程中发生错误或后端崩溃
- **THEN** 系统在恢复后的 snapshot 中保留请求状态、资源停止错误和执行事实，并表达 `stream.failed` 或明确的未确认状态，不能伪装为成功中断

#### Scenario: Crash after interrupt request has no confirmed stop fact

- **WHEN** `interrupt.requested` 已持久化，但后端在 Provider/工具停止和中断终态提交之前崩溃
- **THEN** 恢复流程提交或在 snapshot 中表达 `stream.failed(execution_lost, after_interrupt_requested=true, resumable=false)`；只有停止事实已经确认并持久化时才允许使用 `stream.interrupted`

### Requirement: [Terminal] Terminal events are emitted only after final business validation

系统 MUST 只在 Turn 的最终业务校验、所有必要的工具事实和最终 checkpoint 都成功完成后发布 `stream.completed`。完成、真正中断和失败三类终态 MUST 互斥且幂等。

#### Scenario: Final response is accepted

- **WHEN** AgentLoop 最终校验通过且终态 checkpoint 成功持久化
- **THEN** 系统按顺序关闭当前 block/ModelCall 并发布唯一的 `stream.completed`

#### Scenario: Final validation requires another attempt

- **WHEN** 当前模型结果未通过最终业务校验
- **THEN** 系统发布显式的 retrying/next attempt 状态，不发布 `stream.completed`，并保留前一 attempt 的边界

#### Scenario: Terminal event is retried after a network failure

- **WHEN** 终态已持久化但客户端没有收到 live fanout，随后客户端重连
- **THEN** 服务端通过 replay 或 snapshot 返回同一个终态，不生成第二个逻辑完成结果

#### Scenario: A terminal stream closes its SSE subscription

- **WHEN** SSE 已经发送 `stream.completed`、`stream.interrupted`、`stream.failed` 或表示终态的 snapshot
- **THEN** 服务端结束该订阅；客户端不需要为已终止的消息流保持一个永久连接，也不继续等待该流的新事件

### Requirement: [Frontend] Frontend displays protocol state without becoming its authority

Web 客户端 MUST 以消息流事件更新展示镜像，但不得仅凭本地 React 状态宣布 Turn 成功。前端 MUST 展示 reasoning、文本、工具、中断、失败和重连状态，并在后端状态变化后以完整对象替换对应展示镜像。

#### Scenario: Reasoning and text are rendered independently

- **WHEN** 客户端收到不同 carrier type 的 block.delta
- **THEN** 展示层按 Thinking、Redacted Thinking、文本或工具归类，并保留 block 顺序，不因渲染批处理丢失 delta

#### Scenario: Frontend phase indicators use active_state

- **WHEN** 客户端通过 live event 或 snapshot 更新消息流
- **THEN** 阶段指示器读取 `active_state.kind`、可选的 `active_state.phase`、Activity 通用状态和关联实体 ID；不通过最后一个 block、工具或 Activity 数组推断当前状态，阶段切换后旧实体仍保留在时间线中

#### Scenario: Connection loss is visible but not terminal

- **WHEN** SSE 或消息流连接断开而 Turn 仍未收到权威终态
- **THEN** 前端标记连接异常并保留临时展示，但不把连接断开显示为完成或成功中断

#### Scenario: Backend failure is resolved after reconnect

- **WHEN** 后端重启后前端重新连接
- **THEN** 前端根据 snapshot 显示 `execution_lost`、`outcome_unknown` 或可验证的终态，不永久停留在生成中

#### Scenario: Tool markup is not treated as a workspace file reference

- **WHEN** 未知工具的中间消息包含 XML/function 标记、未知链接或其它不符合工作区路径形态的内容
- **THEN** 前端按普通消息内容展示，不触发工作区文件读取请求；文件引用组件仍必须在最终解析边界再次校验目标，不能仅依赖 Markdown 文本预处理

#### Scenario: Unknown tool outcome remains visible in live and snapshot paths

- **WHEN** `tool.started` 已提交而 `tool.completed` 尚未提交，随后收到 `stream.failed`、`stream.interrupted` 或恢复 snapshot
- **THEN** 前端展示 `outcome_unknown`，不得将该工具显示为成功，也不得把它静默从时间线移除

#### Scenario: Interrupted partial tool call is not shown as running

- **WHEN** 前端收到 `tool_call.completed(status=incomplete|cancelled)`，且该调用从未收到 `tool.started`
- **THEN** 前端展示未完成/已取消的模型工具调用，不创建运行中的 ToolExecution，也不等待一个永远不会到达的 `tool.completed`

#### Scenario: Failed history does not keep an unresolved tool running

- **WHEN** 用户刷新后打开一个已经失败/中断的历史 Turn，历史 response parts 只有 `tool_call` 而没有对应 `tool_result`
- **THEN** 前端将该工具显示为 `outcome_unknown` 且停止运行态，不因该 Turn 已没有 active SSE 而继续显示“正在运行”

#### Scenario: Refresh hydrates an existing message stream snapshot

- **WHEN** 用户刷新页面或重新打开一个已经持久化 message.v1 的历史 Turn，且该 Turn detail 被加载
- **THEN** 前端按 Turn ID 查询一次 snapshot；如果 snapshot 存在，则以 snapshot 完整替换旧 `response_parts` 投影，并恢复工具目标名称、参数、未知结果和失败/中断终态

#### Scenario: A legacy Turn without a message stream is explicit

- **WHEN** 历史 Turn 没有对应的 message.v1 持久化流
- **THEN** snapshot hydration 明确得到“stream 不存在”，客户端仅将该 Turn 作为历史归档 detail 展示，不把旧历史字段当作实时消息流，也不为该 Turn 建立无限重连

#### Scenario: Read APIs do not create a stream

- **WHEN** 客户端对没有持久化 `turn_stream_id` 的 Turn 请求 SSE、事件 replay 或 snapshot
- **THEN** 服务端返回明确的 `404 stream_not_found`，不写入 `stream.opened`、空 checkpoint 或新的消息流索引

#### Scenario: Snapshot hydration and live events share one reducer

- **WHEN** 一次性 snapshot hydration 与活动 Turn 的 live event 在相邻时刻到达
- **THEN** 两者按 `turn_stream_id` 和 `event_seq` 合并；较旧的 snapshot 不得覆盖较新的 live projection，异步 snapshot 到达后必须触发聊天展示更新

#### Scenario: Non-user cancellation is not shown as a user interrupt

- **WHEN** AgentLoop 收到 `CancelledError` 但该 TurnStream 没有已提交的 `interrupt.requested`
- **THEN** 后端记录明确的取消/失败事实，不发布伪造的用户 `stream.interrupted`
