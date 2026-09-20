## Why

当前 Agent 响应链路把上游模型 chunk、Trace 事件、会话持久化和前端时间线更新串在一起，无法稳定表达“刚收到一段 reasoning 就立即展示”，也难以区分用户发起中断、上游真正停止和连接断开。现在引入独立消息协议，可以在不重写既有 `boxteam.workspace.v2` Trace/执行协议的前提下，为一个 Turn 建立可重连、可排序、可细粒度更新的实时响应通道。

## What Changes

- 新增独立协议包 `boxteam.workspace.message.v1`，以 `MessageStreamEvent` 表达一个 Turn 的实时消息流。
- 明确 `Session → UserMessage → Turn → TurnStream` 与 `Turn → Job` 的关系：Turn 是业务回合，Job 是其执行载体，TurnStream 是不随 retry/restart 更换的逻辑消息流；provider 的 `chunk` 只作为内部输入，不暴露给公共协议。
- 支持 `stream.opened`、`model.started`、`model.completed`、`model.retrying`、`block.started`、`block.delta`、`block.completed`、`tool_call.delta`、`tool_call.completed`、工具执行生命周期、通用 `activity.started/updated/completed/failed`、`interrupt.requested`、`interrupt.rejected`、`stream.completed`、`stream.interrupted`、`stream.failed` 和 `stream.snapshot` 等事件。
- 为事件提供稳定的 `turn_stream_id`、单调 `event_seq` 和快照/补偿语义，使前端可以实时消费、断线重连，并在后端重启后获得权威状态。
- 固化 `status`、`outcome`、`completion_reason` 的字段边界；对已启动但结果未知的工具统一使用 `status=completed, outcome=outcome_unknown`。
- 固化 raw chunk 到 AIMessageChunk 的处理顺序：先规范化并完成 event/checkpoint 提交，再 fanout，最后生成 AIMessageChunk 并交给 LangChain 聚合。
- 使用一个统一的 `stream.snapshot` 表达任意时间点的完整 Turn 投影；核心模型/工具阶段继续通过 `active_state.kind/phase` 表达，其他耗时路径统一通过通用 Activity 生命周期表达，不为不同阶段拆分多种 snapshot 事件。
- snapshot 除了使用 `snapshot_seq` 表示消息流整体高水位，还为 ModelCall、MessageBlock、ToolCall、ToolExecution 和 Activity 保留生命周期序号边界（`started_seq`、`last_event_seq`、可选 `completed_seq`）及面向展示的时间字段；连续的上下文压缩使用不同 `activity_id` 并在同一 TurnStream 的 snapshot 中同时保留，不能依赖数组顺序或墙上时间推断全局事件顺序。
- 将 LiteLLM AIMessage `content[]` 中的每个有序 carrier 建模为 `MessageBlock`，保留 `reasoning_content`、`reasoning_items`、`thinking`、`redacted_thinking` 和 `text` 的原始类型与顺序，不把它们过早合并成单一 reasoning 字符串。
- 保持 AIMessage 中的 `tool_calls` 独立于 `content[]`；工具调用消息、工具执行和工具结果不复制成 content block。
- 在 LiteLLM raw stream 迭代和 LangChain `AIMessageChunk` 聚合之间增加规范化 delta hook；同一个规范化 `BlockDelta` 同时驱动实时消息流和 LangChain 聚合，不等待最终 `AIMessage`。
- `MessageStreamWriter` 作为无条件的后端提交点，先持久化 `event_seq`/checkpoint，再按是否存在前端订阅者决定 live fanout；后台任务没有前端连接时不做网络发送，但仍维护消息流状态和终态。
- 已对外发布的 `block.delta` 必须可恢复；raw provider chunk 可以被规范化或合并，不能让未持久化的 delta 先被前端看到。首个语义 delta 发布后不允许静默重试模型调用。
- AgentLoop 的模型、block、工具、重试、中断和 stream 终态事件统一经过同一个 TurnStream 串行提交点；只有 AgentLoop 最终校验通过后才能产生 `stream.completed`。
- 明确任意中断点的未完成事实：运行时仍可提交时，`block.completed` 携带终止原因和 partial 状态，半截 `tool_call` 显式闭合为 incomplete/cancelled，已启动但无法确认的工具实时发布 `tool.completed(status=completed, outcome=outcome_unknown)`；如果进程直接崩溃，则由恢复扫描补出等价权威事实。被用户中断的 ModelCall 必须发布不可重试的取消结果。
- 为模型 attempt 和工具执行结果未知状态提供可恢复语义；后端崩溃后不自动重放可能产生副作用的模型调用或工具调用。
- 明确 event 与 checkpoint 的提交边界和崩溃注入测试，避免前端看到重启后无法恢复的事件。
- 保留既有 `boxteam.workspace.v2` 协议和 Trace 事件中仍被独立能力使用的部分；实时 Web 展示只在迁移期双写，验收完成前删除旧实时适配和回滚开关。
- 将“中断请求已接受”和“执行已真正中断”拆成不同状态，避免把连接关闭或取消请求误显示为最终中断。
- `interrupt.requested` 已持久化但停止事实未确认时，恢复为 `stream.failed(execution_lost, after_interrupt_requested=true)`；只有停止事实已确认并持久化时才发布 `stream.interrupted`。
- 明确后端进程崩溃不依赖发送“崩溃通知”；客户端重连后通过 snapshot 或恢复扫描得到 `execution_lost` 等明确失败状态。
- 为每个 Turn 建立进程内的 `TurnExecutionScope`，按需向 Provider、工具和其它长耗时操作传递其中的取消信号；该 scope 不进入 `message.v1` payload，持久化恢复仍以事件和 checkpoint 为准。
- 将 `CancellationSignal` 限定为取消通知、取消原因和取消 hook；将 deadline、ModelCall/ToolCall 子 scope、临时资源清理和持久资源租约放在 `TurnExecutionScope` 中，避免把 Turn 的全部语义塞进一个通用 token。
- 将持久资源的生命周期从 Turn 执行 scope 中分离：terminal、browser、MCP 连接和开发服务由 `ResourceManager` 持有，Turn 只持有当前操作的 lease；取消 Turn 默认释放操作和 lease，不自动销毁持久资源。
- 为 AgentLoop 增加进程内的类型化 `AgentControlInbox`，承载 interrupt、steer、approval、resume 和资源操作结果等控制输入；宿主 shutdown 仍属于更外层的生命周期信号。它与单次取消信号、消息流输出事件和 ResourceManager 的资源控制分层，避免让一个 token 同时表达多种方向的消息。
- 为 Turn 内非核心模型/工具路径增加通用 Activity 生命周期；未注册的扩展路径默认获得可持久化、可取消、可恢复判断和保守的未知结果处理，注册专用 Handler 后再增加结构化事件、细粒度 snapshot、清理和恢复逻辑。
- Activity Handler 自身失败默认只降级 detail 并继续主 TurnStream；只有底层副作用无法确认时才将 Activity 标记为 `outcome_unknown`/`execution_lost`，禁止虚假成功。

## Capabilities

### New Capabilities

- `turn-message-stream`: 为单个 Turn 提供可重连、可排序、支持 block/delta 和中断状态的实时消息流；Job 作为其执行载体关联该流。

### Modified Capabilities

- 无。既有会话历史和 Trace 能力暂不修改其要求；迁移期通过新增能力并行接入。

## Impact

- 协议：新增 `proto/boxteam/workspace/message/v1` 及其生成代码/序列化边界；snapshot 增加实体生命周期序号和可选时间字段，明确事件日志与当前投影的时序职责。
- 工作区后端：新增 LiteLLM raw delta hook、上游 chunk 规范化、消息流写入器、AgentLoop 生命周期串行器、通用 Activity registry 与默认 Handler、可选语义 Handler、可选 live fanout、事件分发/快照、中断状态协调、TurnExecutionScope、AgentControlInbox、ResourceManager 租约和资源恢复边界；现有 Agent 执行规则保持不变。
- API：新增独立的会话消息流订阅入口，具体 HTTP/SSE 或其他传输方式在设计讨论后确定。
- Web 客户端：新增消息流状态接收与展示路径，复用现有 Turn/ResponsePart 的历史模型；最终不再依赖 Trace 原始事件拼接实时回答。
- 迁移：需要短期双写、事件序号/快照一致性测试、AgentLoop 边界故障注入测试，并在新链路稳定后于本变更内移除旧实时展示路径和兼容代码。
- 前置关系：可复用现有 protobuf 生成和跨进程协议基础，但本变更不修改其既有协议包。
