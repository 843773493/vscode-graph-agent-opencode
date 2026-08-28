## Context

详见 `proposal.md` 的动机。本项目当前已经有 Turn、Job、Trace、SSE 和历史 `ResponsePart` 模型，但实时模型输出仍主要以 Trace 事件和文本 delta 驱动。项目中的 `app/agents/providers/litellm_ai_message_full_example.jsonc` 已经明确了 LiteLLM AIMessage 的实际 carrier 结构：`content[]` 是有序 carrier 序列，reasoning 相关 carrier 保留不同原始字段，`tool_calls` 位于 content 之外。新消息流应以该结构为输入边界，而不是重新拼接成一个扁平 reasoning 字符串。

当前 `BoxteamLiteLLMChatModel` 的流式实现会先把 LiteLLM raw stream 完整收集，再逐项生成 LangChain `AIMessageChunk`；旧事件处理器收到 chunk 后还会按字符数/时间窗口批量发送。这些路径可以继续服务最终 `AIMessage` 和旧 Trace，但不能作为新消息流的实时发送点。

当前约束：

- `Turn` 是持久化的业务回合，`Job` 是其执行载体；本变更不重新定义 Agent 调度和工具执行。
- `boxteam.workspace.v2` 已有消费者，不能把新语义直接塞进旧事件的 `raw_payload` 或改变旧事件含义。
- 前端业务状态仍以后端为权威，实时事件只能更新后端状态的展示镜像。
- 本地运行时不引入消息队列或数据库；实时分发、快照和持久化边界必须适配现有工作区存储与事件总线。

## Goals / Non-Goals

**Goals:**

- 在收到规范化上游 chunk 后，按 block/delta 粒度向前端发布可消费事件。
- 在 LangChain 聚合之前捕获第一个规范化语义 delta，使 reasoning/text 能在上游产生后立即进入消息流。
- 让消息流提交与前端连接解耦：无前端连接时仍提交可恢复状态，有订阅者时才进行 live fanout。
- 让 AgentLoop 的模型、block、工具、重试、中断和 stream 终态事件共享同一个串行提交顺序。
- 让模型 attempt、工具执行未知结果和最终收尾状态在崩溃重启后可被准确恢复。
- 让一个 Turn 的实时流可排序、可重连，并能通过 snapshot 修复事件缺口；Job 只作为执行关联。
- 区分 model call、消息 block、工具生命周期和 stream/turn 终态。
- 区分 `interrupt.requested` 与 `stream.interrupted`，并使迟到的上游 delta 不再污染终态。
- 在后端进程崩溃后，通过重连 snapshot 或恢复扫描得到明确终态，不让前端永久停留在“正在生成”。
- 允许新旧实时链路并行迁移，并保持历史 Turn 的读取模型稳定。

**Non-Goals:**

- 本阶段不迁移所有历史 API，不删除旧 Trace/执行协议，不重做 Agent 调度器。
- 不把 provider 原始 chunk、加密 reasoning payload 或 SDK 私有字段直接变成公共协议。
- 不在本阶段确定完整 UI 视觉改版、跨设备同步或可靠消息队列语义。
- 不承诺每个 provider raw chunk 都作为独立历史记录落盘；但每个已经提交并对外发布的协议 `block.delta` 都必须可通过 event/checkpoint 恢复。
- 不把 provider hook 直接绑定到 SSE/WebSocket，也不在前端连接不存在时跳过后端消息流提交。
- 不在首个语义 delta 已经提交后静默重试同一个 ModelCall；重试必须显式形成新的 attempt/ModelCall，或将当前调用标记为失败。
- 不在工具执行结果未知时自动重放工具；除非未来为工具提供明确的幂等 key 和结果查询协议。

## 文档目录与 canonical 术语

本设计按以下责任域阅读。现有详细故障矩阵和实现约束继续保留在后文，分别作为提交状态机、资源治理、控制输入和 Activity 的专项设计，不重复创建第二套协议。

| 分组 | 内容 |
| --- | --- |
| A. 语义模型与协议边界 | 事件信封、Turn → Job/TurnStream、carrier、MessageBlock、ToolCall |
| B. 数据生产与核心执行 | raw delta hook、LangChain 聚合、ModelCall、ToolExecution |
| C. 一致性与恢复 | checkpoint、snapshot、崩溃扫描、中断请求/事实 |
| D. 传输、前端与迁移 | SSE、replay、前端 reducer、历史 hydration、旧链路清理 |
| E. 详细运行时策略 | delta 提交状态机、scope、资源租约、ControlInbox、Activity Handler |

| 术语 | 唯一含义 | 不表示 |
| --- | --- | --- |
| `Turn` | 一次用户请求对应的业务回合 | 一次 provider 请求或一次 SSE 连接 |
| `Job` | 执行一个 Turn 的后端调度/运行记录 | 新的消息语义层级 |
| `TurnStream` | 一个 Turn 的逻辑消息流；同一 Turn 的 retry/restart 继续使用它 | SSE/TCP 连接或新的 Job |
| `MessageStreamEvent` | `boxteam.workspace.message.v1` 的公共事件信封类型 | `TurnStream` 的别名 |
| `MessageBlock` | AIMessage `content[]` 中一个有序 carrier 的协议投影 | UI 布局块、网络 chunk 或旧 `ResponsePart` |
| `BlockDelta` | MessageBlock 的可排序增量操作 | provider raw chunk |
| `ToolCall` | 模型声明的工具调用 | 工具实际执行 |
| `ToolExecution` | 后端启动的工具执行和结果事实 | 模型的 tool call 参数流 |
| `Activity` | 非核心耗时路径的通用投影 | Job 队列、持久资源或 SSE 连接本身 |
| `scope_ref` | Activity 的公共归属引用 | 运行时 `TurnExecutionScope` 或 `CancellationSignal` |

公共事件字段使用 `status` 表示生命周期，使用 `outcome` 表示结果，使用 `completion_reason` 表示为何闭合；三者不得互相替代。例如未知工具结果写作 `status=completed, outcome=outcome_unknown`，而不是把 `outcome_unknown` 当作生命周期状态。`stream.snapshot` 是带 `event_type` 的协议控制帧：它属于传输事件，但不追加业务日志，也不分配新的 `event_seq`。

## Decisions

### A. 语义模型与协议边界

#### 1. 使用独立协议包和独立事件信封

新增 `boxteam.workspace.message.v1`，公共事件信封固定为 `MessageStreamEvent`。事件信封至少围绕以下关联键组织：`session_id`、`turn_id`、可选的 `job_id`、`turn_stream_id`、可选的 `model_call_id`/`block_id`、`event_id`、单调 `event_seq` 和产生时间。

选择独立包，是为了让新消息语义拥有独立版本和演进边界；不把它扩展成旧 `SessionExecutionEvent` 的更多 oneof，也不继续依赖 `raw_type/raw_payload` 推断语义。旧协议作为迁移适配目标保留。

#### 2. 采用 Turn、Job、TurnStream 分离的业务关系，block 对齐 AIMessage content carrier

语义关系固定为：

```text
Session
└── UserMessage
    └── Turn
        ├── Job（执行载体）
        └── TurnStream（逻辑消息流）
            ├── ModelCall/attempt
            │   ├── content[]
            │   │   └── MessageBlock
            │   │       └── BlockDelta
            │   ├── tool_calls[]
            │   └── response_metadata / usage_metadata
            ├── ToolExecution
            │   └── ToolResult
            └── Activity
```

- `Turn`：一次用户消息触发的业务回合，拥有唯一的逻辑消息流。
- `Job`：执行 Turn 的后端调度/运行记录；Job retry、恢复或重启不新建 TurnStream。
- `turn_stream_id` 是消息流 writer、checkpoint、replay 和 snapshot 的主键；Job 的变化只通过可选 `job_id` 关联，不改变消息流身份。
- `TurnStream`：该 Turn 的实时响应句柄，不是 SSE/TCP 连接，也不是新的 Job；保留独立 ID 以承载重连游标和快照版本。
- `ModelCall`：一次上游模型请求。工具循环、重试或续接可能产生多个 ModelCall。
- `MessageBlock`：`AIMessage.content[]` 中一个有序 carrier，连续且可独立收尾；首版 carrier type 对齐样例中的 `reasoning_content`、`reasoning_items`、`thinking`、`redacted_thinking` 和 `text`。
- `BlockDelta`：对一个 MessageBlock 的有序增量；可以是文本追加，也可以是结构化 reasoning item 更新。
- `tool_calls[]`：模型在 AIMessage 上声明的工具调用集合，不复制到 `content[]`；工具实际执行通过 `ToolExecution` 和 `ToolResult` 表达。
- `chunk`：provider/SDK 的传输碎片，只在内部规范化层存在。

`block` 在这里不是网络 chunk，也不是 UI 布局块，而是 LiteLLM AIMessage content carrier 的协议化名称。保留 carrier type 和顺序，可以避免丢失 `thinking` 的签名、`reasoning_items` 的结构、`redacted_thinking` 的不可见状态，也避免把相邻但语义独立的 text carrier 强行合并。

#### 3. block/delta 使用显式生命周期和 carrier 类型

首版事件按生命周期拆分：`block.started` → 一个或多个 `block.delta` → `block.completed`。事件至少携带 `block_id`、`block_index`、`carrier_type`、局部序号和明确的操作类型。`block.completed` 必须携带 `completion_reason` 和 `partial`：正常上游结束使用 `upstream_completed`，用户中断使用 `user_interrupt`，Provider 异常使用 `provider_failed`，无法恢复使用 `execution_lost`；只要未收到该 block 的正常完成边界，`partial` 就必须为 true。

按样例处理 carrier：

- `reasoning_content`、`thinking` 和 `text` 使用文本追加 delta，但保留各自的 `carrier_type`。
- `reasoning_items` 使用结构化 item upsert/patch；item 内的 `content[].type=reasoning_text`、`summary[].type=summary_text`、`status` 和 `id` 不被压平成普通文本。
- `redacted_thinking` 只产生不可见/已脱敏的 block 状态或 marker，不能把 `data` 当作前端可读 reasoning。
- 相邻的多个 `text` carrier 在后端保持独立；前端可以为渲染合并，但不得改变事件顺序和 block 边界。

当 carrier 在模型流中切换时，规范化器必须先闭合前一个 block，再为新 carrier 创建新的 block；Provider 的空 metadata chunk、usage chunk 或 finish-only chunk 不得凭空创建可见 block.delta。若上游只提供一个完成边界而没有 block 级完成信息，规范化器必须将当前 block 标记为 `upstream_completed`；若中断闸门已经打开，则只能使用 `user_interrupt` 或 `execution_lost`，不能把半截内容误标为正常完成。

`tool_call` 不作为 content carrier kind；它来自 AIMessage 的独立 `tool_calls[]`。工具调用需要自己的生命周期和 ID，工具执行结果也不能伪装成模型 text block。

#### 4. Provider carrier 与前端展示语义分离

后端保留 LiteLLM carrier 的原始结构和 provider state，但公共前端投影可以把多个 carrier 映射成统一的展示分类：

```text
reasoning_content ─┐
thinking           ├─→ 前端 Thinking 展示
reasoning_items ───┘
redacted_thinking ───→ 前端 Redacted Thinking 展示
text ────────────────→ 前端文本展示
tool_calls[] ────────→ 前端工具调用展示
```

`thinking.signature`、`reasoning_items[].encrypted_content` 和 provider-specific `additional_kwargs` 属于后端 provider state。它们可以为了后续 LiteLLM/Anthropic 请求被持久化和回放，但不得默认进入前端公共 projection。LiteLLM 的 `reasoning_content` 是跨 provider 的统一字段，而 `thinking_blocks` 主要是 Anthropic 结构；因此协议保留样例 carrier type，展示层再做语义归类。[LiteLLM reasoning content 文档](https://docs.litellm.ai/docs/reasoning_content)

### B. 数据生产与核心执行生命周期

#### 5. 在 LangChain 聚合前使用规范化 delta hook

实时消息流必须位于 LiteLLM raw stream 迭代和 LangChain `AIMessageChunk` 聚合之间。provider 层不直接连接 SSE/WebSocket，而是通过当前执行上下文注入一个后端 `UpstreamDeltaHook`，将同一份规范化结果交给消息流 writer 和 LangChain adapter：

```text
LiteLLM raw chunk
    ↓
规范化为 NormalizedModelDelta
    ├── MessageStreamWriter.accept()
    │   ├── DurableStreamStore
    │   └── LiveSubscriberHub（可为空）
    │
    └── 转换为 AIMessageChunk
        ↓
    LangChain 聚合为最终 AIMessage
```

`NormalizedModelDelta` 是唯一的 provider 到业务边界，至少携带 `model_call_id`、`block_id`、`block_index`、`carrier_type`、delta 操作、provider state 增量和原始到达顺序。消息流和 LangChain 不得各自重新解析 LiteLLM raw chunk，避免两条路径的 block 身份和顺序漂移。

`MessageStreamWriter.accept()` 是每个 TurnStream 的必经提交点：

1. 进入该 `turn_stream_id` 的单写入队列/串行器。
2. 检查中断闸门和 stream 终态；已终止的 stream 拒绝迟到 delta。
3. 分配单调 `event_seq`，更新对应的 block、ModelCall、ToolExecution 或 AgentLoop projection。
4. 以统一提交单元持久化 event 和 checkpoint；在提交完成前不得把 `event_seq` 暴露给前端。
5. 持久化成功后，把事件放入 `LiveSubscriberHub`；没有前端订阅者时 fanout 是空操作。
6. 如果事件来自 LiteLLM raw delta，返回给 LangChain adapter，使其继续产生 `AIMessageChunk`。

这里的串行器不只处理 `block.delta`，还必须处理 `block.started/completed`、`tool_call.delta/completed`、`model.started`、`model.completed/failed`、`model.retrying`、`tool.started`、`tool.completed`、`interrupt.requested`、`stream.completed` 和 `stream.failed`。任何绕过该串行器的旧 `TEXT_END`、`AGENT_END` 或中断终态发布，都可能在 delta 之间制造不可解释的顺序。

因此，“是否有前端连接”只影响网络 fanout，不影响消息事件的语义提交。后台任务没有前端连接时仍然经过同一 writer，保留 block 状态、stream 终态和可生成 snapshot 的 checkpoint；如果未来需要观察该任务，可以从当前状态重连。

已提交并可被前端观察的 `block.delta` 必须先可恢复。raw provider chunk 可以在规范化时合并或转换，但不能先把未持久化的协议 delta 发给前端。前端订阅者使用有界队列；慢客户端不得阻塞 LiteLLM，队列溢出时断开并要求客户端通过 `event_seq`/snapshot 修复缺口。

旧 Trace writer、新消息流 writer 和最终 AIMessage 持久化都从同一个规范化 delta 输入派生，避免把 LangChain 聚合结果当成实时消息流来源。

异步 Provider 的读取必须经过统一的 `CancelableStream`，不能由每个 Provider 重新实现一套 `asyncio.wait` 或只依赖 `aclose()` 唤醒挂起的 `anext()`：

```text
CancelableStream(raw_stream, turn_signal)
    ├── next_task = anext(raw_iterator)
    ├── cancel_task = turn_signal.wait()
    └── FIRST_COMPLETED
          ├── next_task → 取消 cancel_task，返回 raw chunk
          └── cancel_task → cancel next_task → await upstream close
                                      → await next_task 收尾
                                      → 抛出 ScopeCancelledError
```

- 取消分支必须主动取消 pending `anext()` 任务，调用并等待 Provider 的 `aclose()` 或 `close()`，再等待读取任务真正结束；关闭失败不能被吞掉，必须进入 AgentLoop 的失败/执行丢失路径。
- raw chunk 被读取后，当前 chunk 的规范化、writer 提交、诊断 fanout、LangChain callback 和 `AIMessageChunk` yield 在当前消费任务中连续完成。读取器不得以 `task.cancel()` 粗暴打断这段处理；处理完成后下一次读取才响应已经到达的取消信号。
- `interrupt.requested` 与当前 chunk 的 writer 提交仍由同一 `TurnStream` 串行化：delta 先线性化则保留并继续交给 LangChain，interrupt 先线性化则拒绝后续 delta。读取器负责停止上游读取，AgentLoop 负责补齐 block、ModelCall、工具和最终 `stream.interrupted`/`stream.failed`。
- LiteLLM Chat、LiteLLM Responses 和 Anthropic SDK raw `AsyncStream` 都复用这一读取器。Anthropic 不能只包住已经转换后的 `ChatGenerationChunk` generator，否则无法保证底层 HTTP/SSE response 被关闭。
- 读取器以异步上下文管理器覆盖正常结束、Provider 异常、消费者提前退出和外层 `task.cancel()`；关闭动作幂等。没有 Turn signal 的直接模型调用仍使用同一关闭边界，但跳过取消竞速。同步 Provider 没有可等待的 `anext()`，继续使用同步逐 chunk 路径和同步 close 兜底。

当前实现的接线约束是：所有生产 provider wrapper（LiteLLM Chat、LiteLLM Responses 和 Anthropic）在收到 LiteLLM raw chunk 后、生成 `AIMessageChunk` 之前调用执行上下文中的 `UpstreamDeltaHook`；hook 先产出 `NormalizedModelDelta` 并交给 `MessageStreamRuntime.accept_normalized_delta()`，提交成功后才允许继续生成 `AIMessageChunk`，再交给 LangChain callback/聚合。随后由 `MessageStreamTraceObserver` 从同一份已提交规范化结果生成旧 Trace 的诊断投影。`AgentEventStreamProcessor` 只负责从同一模型事件流提取最终文本、工具和业务校验，不再自己生成第二套 `TEXT_START/TEXT_DELTA/TEXT_END`。因此旧 Trace 与新消息流共享 raw provider hook 的 block 身份和到达顺序，聊天 Web 不再消费 Trace 诊断投影。

runtime 同时保留当前 ModelCall 的规范化可见文本，AgentLoop 在每次模型结果校验前与事件流聚合文本对账；只记录长度和关联 ID 的 warning，不把诊断分支变成第二个发送源，也不因前端是否连接而改变执行结果。

如果某个测试替身没有安装该 hook，它只能验证最终聚合/业务事件，不能把 `on_chat_model_stream` 重新当成消息流回退入口；生产 provider wrapper 缺少 hook 必须显式失败。这样可以避免真实运行时出现“双提交”或一条链路丢 delta、另一条链路补 delta 的不一致。

#### 6. AgentLoop 的 ModelCall attempt 与最终收尾

一次 AgentLoop 可能因为空回复、缺少工具调用、缺少委派报告或其它业务校验而重新请求模型。每次真实上游请求都必须有独立的 `model_call_id`/attempt，不得把多个请求的 block 合并成同一个 ModelCall。

每个 ModelCall 至少有以下结果之一：

```text
model.completed(outcome=accepted)
model.completed(outcome=validation_failed)
model.failed(outcome=user_interrupt | provider_error | execution_lost)
model.retrying(reason=...)
```

用户中断导致 Provider stream 被关闭时，当前 ModelCall 必须提交 `model.failed(outcome=user_interrupt, retryable=false)`；它表示该 attempt 被用户主动停止，不表示 Provider 返回了一个完整的模型结果。没有 `interrupt.requested` 的外部取消或 `CancelledError` 不得复用这个 outcome，而应记录为 `execution_cancelled`/`external_shutdown` 等明确原因。

如果某次 ModelCall 已经产生最终文本，但 AgentLoop 校验发现还需要重试，该文本仍属于该次 ModelCall；它不能直接被标记为 Turn 的最终答案。可以通过 `projection=intermediate` 或 `status=superseded` 标记，前端是否展示由 projection 决定。只有最终业务校验通过并完成最终 checkpoint 后，才允许提交 `stream.completed`。

如果后端在 ModelCall 完成和 retry 决策之间崩溃，snapshot 必须返回 `agent_loop_status=validating` 或 `retrying` 以及当前 `model_call_id`，不能把上一轮文本误报为 Turn 完成。

#### 7. ToolExecution 的未知结果和副作用边界

模型产生的 `tool_calls[]`、后端实际启动工具、工具返回 `ToolResult` 是三个不同边界：

```text
tool_call.delta/completed
    → tool.started
    → tool.completed(status=completed | failed, outcome=success | outcome_unknown)
```

`tool_call` 自身也必须有生命周期终点。参数仍在流式接收时被中断，不能只拒绝后续 `tool_call.delta`；writer 必须为已经创建的调用提交：

```text
tool_call.completed(
    status=incomplete,
    completion_reason=user_interrupt,
    arguments_complete=false
)
```

如果工具调用已经完整但尚未启动执行，则中断时提交 `status=cancelled, completion_reason=user_interrupt`，不得产生 `tool.started`。如果 `tool.started` 已经持久化，工具执行则必须单独提交 `tool.completed`；取消后无法确认结果时也必须实时提交 `status=completed, outcome=outcome_unknown`，不能只在 checkpoint 内把 running 标记改掉，否则在线客户端在收到 `stream.interrupted` 后仍可能把工具显示为运行中。

工具调用的关联必须跨越 provider delta 和 AgentLoop 事件：同一个模型工具调用的所有分片都使用同一个 provider `tool_call_id`，后续分片缺少 `name` 或参数仍不得清空已经收到的名称和参数；`tool.started`/`tool.completed` 使用独立的 `tool_execution_id`，但其 `tool_call_id` 必须指向对应的 provider 调用。对于 `invoke_custom_tool` 这类入口，`tool_calls[].tool_name` 保留模型实际调用的入口名称，`tool_executions[].tool_name` 可以是解析后的目标工具名，目标工具名和参数仍从同一 `tool_call_id` 的 `tool_calls[]` 恢复。这样实时投影、snapshot 和刷新后的历史投影不会因执行 run id 与 provider call id 不同而丢失参数。

如果后端在 `tool.started` 之后、`tool.completed` 持久化之前崩溃，恢复扫描必须将该 ToolExecution 标记为 `status=completed, outcome=outcome_unknown`。本变更默认不得自动重放，因为工具可能已经写文件、发送消息或修改外部状态。

只有工具明确提供幂等 key、结果查询或事务回滚能力时，未来变更才能增加自动恢复。`stream.interrupted` 也不能掩盖一个尚未确认结果的工具执行；此时应优先保留 `status=completed, outcome=outcome_unknown` 和相应失败/恢复状态。

### C. 一致性、快照与中断事实

#### 8. 后端崩溃采用恢复快照，不依赖崩溃通知

后端进程可能在发送任何终态事件前退出，因此协议不依赖 `backend.crashed` 这类必须在崩溃前发出的消息。后端重启后由恢复扫描读取持久化的 Turn、关联 Job 和消息流检查点，并在客户端重连时通过 snapshot 返回权威状态。

snapshot 至少包含：

```text
turn_stream_id
snapshot_seq
stream_status
agent_loop_status
active_state
current_model_call_id
current_attempt
blocks[]
tool_calls[]
tool_executions[]
activities[]
resource_refs[]
interrupt_state
failure
recovery
resumable
```

`active_state` 使用一个带判别字段的联合结构，而不是四套阶段 snapshot，也不把所有耗时路径都扩展成顶层 phase：

```text
ActiveState
├── kind = model_output
│   ├── phase = reasoning | text
│   ├── entity_id
│   ├── carrier_type?
│   ├── block_id?
│   └── status
├── kind = tool_call
│   ├── phase = accumulating | stopping
│   ├── tool_call_id
│   └── status
├── kind = tool_execution
│   ├── phase = running | stopping
│   ├── tool_execution_id
│   └── status
├── kind = activity
│   ├── activity_id
│   ├── activity_kind
│   ├── status = running | waiting | stopping | completed | failed | unknown
│   ├── summary?
│   └── detail_ref?
└── kind = interrupting | terminal
    ├── last_kind
    ├── last_phase?
    ├── reason
    └── entity_id?
```

其中 snapshot 是完整的当前展示投影，不是只补文本的增量：

- `active_state` 是当前活动实体的明确指针，不是新的业务事实。核心模型/工具实体必须带 `kind`、`phase`、关联实体 ID 和当前状态；`model_output` 的 reasoning/text 阶段必须带 `block_id` 与 `carrier_type`，`tool_call` 阶段必须带 `tool_call_id`，`tool_execution` 阶段必须带 `tool_execution_id`。非核心耗时路径使用 `kind=activity`，通过 `activity_id`、`activity_kind`、通用状态和可选 `detail_ref` 表达，不再为每一条 AgentLoop 路径新增顶层 phase。`interrupting`/`terminal` 阶段还必须带 `last_kind` 和必要的 `last_phase`。
- `blocks[]` 必须包含 block 身份、顺序、carrier type、当前内容/结构化 item、生命周期状态、`completion_reason` 和 `partial`；reasoning/text 被中断时，前端可以直接从这里恢复半截内容和中断原因。
- `tool_calls[]` 必须包含 `tool_call_id`、已确认的名称和参数、参数是否完整、调用状态以及 `completion_reason`；`incomplete`/`cancelled` 的调用不能被前端投影为 running ToolExecution。
- `tool_executions[]` 必须包含 `tool_execution_id`、关联 `tool_call_id`、工具目标、执行状态和结果状态；`outcome_unknown` 是权威状态，不得由前端根据缺失 `tool.completed` 自己猜测。
- `current_model_call_id`、`current_attempt` 和 `agent_loop_status` 用于恢复模型 attempt；如果用户中断导致 ModelCall 未正常结束，snapshot 必须包含 `model.failed(outcome=user_interrupt, retryable=false)` 的等价状态。
- `interrupt_state` 必须保留 `interrupt_request_id`、请求状态、原因以及是否已经进入事实终态；`stream.interrupted` 不得被压缩成一个没有请求关联的布尔值。
- `resource_refs[]` 只包含关联资源、operation lease 和操作状态，不复制 terminal/browser/server 的完整资源详情；资源详情继续由 ResourceManager 查询。
- `activities[]` 包含当前 Turn 内已经登记的通用 Activity 投影；未注册 Handler 的 Activity 只保证通用生命周期和安全恢复字段，专用结构通过统一 `stream.snapshot` 内的 `detail_ref` 或可选 projection 提供。
- `model_calls[]`、`blocks[]`、`tool_calls[]`、`tool_executions[]` 和 `activities[]` 中的每个实体都必须保留生命周期时序元数据：`started_seq`、`last_event_seq`，以及实体已闭合时的 `completed_seq`；`started_at`、`updated_at`、`completed_at` 是可选的展示时间字段。序号使用同一 `turn_stream_id` 下的已持久化 `event_seq`，时间字段不能替代序号进行排序。

`active_state` 只解决“snapshot 截取时当前在哪个阶段”，不替代完整数组：

```text
blocks[] / tool_calls[] / tool_executions[] / activities[] = 截止 snapshot_seq 的完整投影
active_state                                                   = 当前活动实体指针
```

因此同一个 Turn 可以在 reasoning、text、tool_call、tool_execution 或任意通用 Activity 状态的不同时间点产生多个 `stream.snapshot`，每个 snapshot 使用不同的 `snapshot_seq`；前端不需要维护按阶段拆分的 snapshot reducer。

snapshot 只保留截至 `snapshot_seq` 的当前投影和实体生命周期边界，不等价于完整事件日志：

- 同一 Turn 内的连续上下文压缩必须创建不同的 `activity_id`。`activities[]` 同时保留每个压缩 Activity 的最终状态和生命周期序号，不得按 `kind=context.compaction` 合并成一条记录。
- snapshot 在第一次压缩完成、第二次压缩进行中时，必须能够表达“第一个 Activity 已完成、第二个 Activity 仍在运行”，并让 `active_state.activity_id` 指向第二个 Activity。
- `started_seq`、`completed_seq` 和 `last_event_seq` 用于恢复实体之间的生命周期先后；`activities[]`、`model_calls[]` 和 `blocks[]` 的数组排列不构成跨实体全局时序契约。
- 如果客户端需要恢复 Activity 的中间进度或 delta 级历史，必须使用 event log replay；不能要求 snapshot 保存所有 `activity.updated` 或 `block.delta` 历史，也不能从 `updated_at` 推断事件先后。

推荐的实体时序投影如下：

```text
EntityProjection
├── started_seq       # 首次生命周期事实的 event_seq
├── last_event_seq    # 最近一次更新该实体的 event_seq
├── completed_seq?    # 终态闭合事实的 event_seq
├── started_at?       # 展示用时间
├── updated_at?       # 展示用最近更新时间
└── completed_at?     # 展示用终态时间
```

其中 `snapshot_seq` 是整个投影的高水位，实体序号是该高水位内的定位信息；二者不能互相替代。snapshot 生成时不新增业务事件，因此 snapshot 自身的传输时间不作为任何实体的生命周期时间。

`stream.snapshot` 的 envelope `event_seq` 必须等于 payload 的 `snapshot_seq`。snapshot 是由持久化 checkpoint 生成的协议控制帧，不是追加到事件日志中的新业务事实；它表示该序号对应的持久化高水位投影，不额外消耗一个新的 event_seq。客户端应用 snapshot 后将游标推进到 `snapshot_seq`，服务端只发送大于该序号的事件。相同 `snapshot_seq` 的 snapshot 可以因重连重复发送，即使每次传输的 `event_id` 不同，前端也必须按 stream 和序号幂等处理。

后端崩溃不发送 `backend.crashed` 事件；恢复扫描和重连 snapshot 承担崩溃后的事实查询。对单个 delta 的崩溃边界必须保持透明：

- raw delta 尚未进入 writer、或尚未分配 `event_seq`：该 delta 不存在，不伪造补发。
- `event_seq` 已分配但 event/checkpoint 尚未持久化：该 delta 不存在，stream 由恢复流程决定是否失败。
- event/checkpoint 已持久化但尚未 fanout：重连时通过 replay/snapshot 补回。
- 已持久化的事件不得因为进程崩溃而从 snapshot 中消失。
- 不允许先 fanout、后持久化；前端不能看到一个重启后无法恢复的已提交 delta。
- `tool.started` 已持久化但 `tool.completed` 未持久化：工具结果为 `status=completed, outcome=outcome_unknown`，不得自动重放。
- 持久资源的操作 lease 已持久化但操作结果未确认：只恢复 `resource_id`、lease 和操作的未知状态，由 ResourceManager reconcile；不把资源误标记为已销毁，也不自动重放操作。
- ModelCall 已产生文本但 AgentLoop 尚未完成校验：恢复为当前 attempt 的中间状态，不得直接生成 `stream.completed`。

客户端携带 `after_seq` 重连时：如果缺失事件仍在实时缓存中，服务端补发缺失事件；如果事件已经丢失，则先返回完整 snapshot，再发送 `snapshot_seq` 之后的新事件。前端必须用 snapshot 替换该 stream 的 live projection，再应用更新事件，不能继续保留未经后端确认的旧 delta。snapshot 不能只追加 blocks 或修补文本，必须完整替换 blocks、tool_calls、tool_executions、ModelCall、AgentLoop、interrupt、resource_refs 和 failure projection。

如果持久化状态已经证明 Turn 正常完成或已真正中断，恢复扫描可以补发对应终态；如果最后状态仍是运行中，但没有足够的执行检查点证明可以继续，则本变更默认标记：

```text
stream.failed
failure.code = execution_lost
failure.after_interrupt_requested = true | false
resumable = false
```

本地后端不在本变更中自动重放或恢复丢失的上游模型调用；自动恢复执行需要另行设计，避免把重复工具调用或重复写入伪装成正常续接。

#### 9. 中断采用请求态与事实态两阶段

用户操作先产生带 `interrupt_request_id` 的 `interrupt.requested`，并将该请求放入与所有 AgentLoop 事件相同的 TurnStream 串行器；只有请求事件持久化后，服务端才通过对应 Turn 的 `TurnExecutionScope` 发出取消信号，取消当前操作并释放其资源 lease。相同请求重试必须幂等。持久资源是否销毁由 `ResourceManager` 根据资源生命周期和显式 stop 命令决定，不能由 Turn 取消隐式决定。

delta 和 interrupt 的先后以该串行器的线性化顺序为准：

```text
block.delta seq=41
interrupt.requested seq=42
→ seq=41 生效，seq=42 之后的迟到 delta 被拒绝
```

底层取消是尽力而为，但 writer 进入中断闸门后不得再提交新的 block 或 tool_call delta。已创建但未完成的 block 必须提交 `block.completed(completion_reason=user_interrupt, partial=true)`；仍在接收参数的 tool call 必须提交 `tool_call.completed(status=incomplete, completion_reason=user_interrupt, arguments_complete=false)`；已经完整但尚未启动的 tool call 必须提交 `tool_call.completed(status=cancelled, completion_reason=user_interrupt)`。当前 ModelCall 在 Provider stream 关闭后提交 `model.failed(outcome=user_interrupt, retryable=false)`。这些收尾事实持久化后，才发布带相同请求 ID 的 `stream.interrupted`。

如果停止过程中发生真实错误，或后端在确认取消前崩溃，错误终态必须保留错误语义，不伪装成中断成功；进程仍可写入时，已启动工具必须先实时提交 `tool.completed(status=completed, outcome=outcome_unknown)`，再提交 `stream.failed`。只有 Provider 和工具都确认停止且没有其它错误时，才提交带未知工具结果的 `stream.interrupted`。进程直接崩溃时，恢复扫描必须补出同等的工具未知事实。

中断与最终收尾也必须服从同一顺序：

```text
stream.completed 已提交
    → interrupt.requested 记录为 interrupt.rejected(reason=already_terminal)

interrupt.requested 已提交
    → 阻止后续 stream.completed
    → 等待模型/工具停止
    → stream.interrupted 或 stream.failed
```

如果中断发生在工具执行期间，必须先确认 AgentLoop 不会继续驱动该工具操作；工具结果即使未知，也可以在停止事实已确认后发布 `stream.interrupted`，但必须同时保留 `outcome_unknown`。`stream.interrupted` 只表示 Turn 因用户请求停止，不表示工具副作用已回滚、持久资源已销毁或工具结果成功。

这比单独关闭前端 SSE 或立即发布旧的 `session_interrupted` 更准确；迁移期旧事件可以由新终态适配生成。

### D. 传输、前端与迁移

#### 10. 前端增加消息流消费路径

Web 客户端新增独立的 message-stream hook/store：

- 连接层负责游标、重连、缺口检测和 snapshot 请求。
- 状态层按 `turn_stream_id`、`block_id` 和 `event_seq` 做幂等合并。
- 收到 `stream.snapshot` 时，reducer 按 `snapshot_seq` 做完整替换：小于当前已应用序号的 snapshot 丢弃；等于当前序号的 snapshot 只做幂等校验/替换；大于当前序号的 snapshot 替换整份 projection，并将游标推进到该序号。
- snapshot 与 live event 并发到达时，连接层先注册订阅再读取 snapshot/replay，并暂存尚未能连续应用的 event；snapshot 应用后只释放 `event_seq > snapshot_seq` 的事件，`event_seq <= snapshot_seq` 的事件丢弃或按 event_id 去重。
- 前端阶段指示器直接读取 `active_state.kind`、可选的 `active_state.phase` 和 Activity 通用状态；不得通过扫描最后一个 block、tool call、ToolExecution 或 Activity 数组推断当前状态。阶段切换时，旧实体继续保留在完整 projection 中，只有 `active_state` 指向新的活动实体。
- 展示层将 live block 投影为现有 `ResponsePart`/timeline item，按 carrier type 归类为 Thinking、Redacted Thinking、文本和工具展示。
- 展示层对来自工具中间消息的 XML/function 标记、未知链接和其它非路径内容执行最终文件引用校验；只有明确符合工作区路径形态的目标才允许触发文件读取，避免错误展示把协议内容变成无效网络请求。
- 多个 delta 在浏览器帧或现有短批窗口内合并提交 React，协议事件本身不因渲染节流而丢失。
- snapshot 恢复时同时替换 AgentLoop 状态、当前 ModelCall attempt、blocks、ToolExecution、activities 和中断状态，不能只恢复文本。
- snapshot 恢复时同时替换 partial block、incomplete/cancelled tool call、`outcome_unknown` ToolExecution、ModelCall failure、resource_refs 和连接后的终态，不允许沿用旧 projection 中已经不存在的 running 项。
- 没有前端连接时不产生 live 网络发送；前端稍后接入时以 replay/snapshot 获取已经提交的状态。
- SSE 断开或后端不可达时保留临时展示并标记连接状态，但不得把临时内容视为完成；重连 snapshot 是最终覆盖来源。
- snapshot 的 `stream_status` 为 terminal 时，前端应用完整 projection 后停止该 stream 的自动重连；为 open/running/interrupting 时，前端保留连接状态并继续消费大于 `snapshot_seq` 的事件。
- 历史加载继续使用权威 Turn detail；实时路径不再通过 `trace.observed` 和旧 `text_delta` 反推聊天消息。
- 历史 Turn 如果已经是失败/中断终态但旧 `response_parts` 只留下工具开始、没有工具结果，前端只能将该工具投影为 `outcome_unknown`；不得因为当前页面没有 active SSE 而继续显示 `running`。如果同一 Turn 已有消息流 snapshot，则 snapshot 投影优先于旧历史部件。

历史 Turn 的恢复入口也必须明确分层：页面刷新后不能假设前端仍然持有运行期间的内存镜像，也不应为所有历史 Turn 永久建立 SSE。Turn detail 被加载或重新加载时，客户端应按其中的 Turn ID 查询一次消息流 snapshot；存在 snapshot 时，将它作为该 Turn 的完整展示镜像并覆盖旧 `response_parts`，然后再由运行中的 Turn hook 负责继续订阅增量。snapshot 不存在时返回明确的“该存量 Turn 没有 message.v1 流”结果，才允许继续使用权威历史 detail 作为归档展示来源；这不是实时链路的静默回退。消息流状态 Map 必须参与聊天投影的响应式依赖，保证异步 snapshot 到达后立即重绘，而不是只在下一次历史请求时生效。

因此 Web 的连接生命周期分为两条明确路径：活动 Turn 使用 `turn_stream_id + after_seq` 的长连接；历史/刷新入口使用一次性 snapshot hydration。两条路径都经过同一个 reducer，snapshot hydration 不重新拼接文本、不生成新的事件序号，也不能覆盖更新的活动流状态；如果同一 Turn 已有更高 `snapshot_seq` 或 live event，必须按 stream 身份和序号保留更新版本。

#### 11. 双写只保留诊断投影，Web 实时源完成切换后移除兼容链路

迁移阶段由同一份规范化上游结果同时驱动新消息流和旧 Trace/执行诊断事件；旧 Trace 只用于事件队列、请求日志和历史诊断，不再作为 Web 聊天实时回答的候选源。新流出现协议错误、序号缺口或终态不一致时，前端显示连接/协议错误并请求 snapshot，不能静默回切旧展示路径。

双写只作为迁移阶段，不把两个协议永久合并。当前收尾实现已经删除聊天实时路径中的旧 `TEXT_DELTA/TEXT_END` 聚合器、live Trace→ResponsePart 适配和消息流静默回退；`AGENT_END` 及其它旧事件仍可被目标、事件队列和请求日志等独立能力使用，但不再驱动聊天正文。与 Trace、历史读取等无关的 `boxteam.workspace.v2` 能力不在删除范围内。

#### 12. 首版传输、身份、快照和 delta 操作收敛为项目默认

为避免实现阶段在协议边界上继续分叉，首版采用以下确定约束：

- 对外订阅复用现有 SSE 事件总线和 Gateway 代理，不新增 WebSocket 或第二套实时传输；协议事件的 JSON 表示与 protobuf 类型保持一一对应。
- 消息流的读取 API（SSE 订阅、事件 replay、snapshot）必须使用只读的 `open_existing` 语义：Turn 没有已持久化的 `turn_stream_id` 时返回明确的 `404 stream_not_found`，不得因历史查询创建 `stream.opened` 或空 checkpoint。只有 AgentLoop 首次启动消息流和用户中断写入路径允许使用创建型 `open`。
- 一个 Turn 默认只创建一个 `turn_stream_id`；模型 retry、工具循环、上下文压缩和后端恢复都属于该 stream 的不同 ModelCall/attempt 或 Activity，不通过新建 Turn 或 stream 隐藏同一 Turn 的连续性。只有明确开启新的用户 Turn 时才创建新的 stream。
- 每个已提交事件都更新可恢复 checkpoint；stream.opened、模型/工具生命周期、中断和终态必须立即提交，普通 delta 也必须在提交边界内更新当前 block 投影。checkpoint 可以压缩历史事件，但不得改变已提交的 event_seq。
- 文本 carrier 首版使用 append delta；`reasoning_items` 使用结构化 item append/upsert/patch。replace 只作为内部恢复操作，不作为前端可见的无来源覆盖。
- `block.completed` 必须携带 `completion_reason` 和 `partial`；中断、Provider 失败、执行丢失不得复用正常上游完成语义。半截 `tool_call` 必须有 `tool_call.completed(incomplete|cancelled)`，不得仅依靠 snapshot 中仍存在一个 running 调用来表达中断。
- `tool.started` 之后若结果无法确认，AgentLoop 在进程仍可提交时必须发布 `tool.completed(status=completed, outcome=outcome_unknown)`；如果进程直接崩溃，则恢复扫描必须补出同等的实时/恢复事实，保证 snapshot 和 live reducer 都不会继续显示 running。
- 用户中断关闭 ModelCall 时必须发布 `model.failed(outcome=user_interrupt, retryable=false)`；外部取消、宿主 shutdown 和 Provider error 使用不同 outcome，不能都映射成用户中断。
- snapshot 是当前权威投影，不承诺永久保存所有原始 delta；在事件被压缩前，服务端必须能以 snapshot 替代缺失区间，且 snapshot_seq 之后的事件仍保持连续。
- snapshot 必须保留每个已出现的 ModelCall、MessageBlock、ToolCall、ToolExecution 和 Activity 的生命周期序号边界；连续同 kind Activity 不能覆盖彼此。跨实体的严格事件顺序仍以事件日志的 `event_seq` 为准，snapshot 不承诺重放已经省略的中间更新事件。
- snapshot 的 `session_id`、`turn_id`、`turn_stream_id` 使用事件信封字段承载；内部 checkpoint 即使包含这些定位字段，编码为 `StreamSnapshot` 时也必须剥离，避免把存储投影和公共 payload 字段混淆。
- 对外 JSON/SSE 编解码必须把 `event_seq` 和 `snapshot_seq` 归一化为前端可比较的非负整数；Protobuf JSON 对 `int64` 默认产生的十进制字符串不能直接泄漏到 Web reducer，否则 snapshot 后的 `+1` 游标检查会制造伪缺口。
- 终态事件或终态 snapshot 发出后，SSE 订阅必须正常结束；客户端不应依赖服务端永久保持一个已经完成的连接，也不应在终态后继续等待新的 delta。
- `stream.failed`/`stream.interrupted` 的 checkpoint 会把仍为 running 的 ToolExecution 改为 `status=completed, outcome=outcome_unknown`；前端实时 reducer 与 snapshot 投影都必须保留该枚举，不能只渲染成普通失败，更不能显示为成功。
- `CancelledError` 只有在同一 TurnStream 已存在 `interrupt.requested` 时才能映射为 `stream.interrupted`；没有用户中断请求的取消必须进入明确的失败/取消事实，避免把进程排空、服务重启或外部取消误显示成用户主动打断。

### E. 详细运行时策略

#### 13. 任意 delta 边界的提交状态机与恢复矩阵

为了避免“事件已经展示但重启后不存在”或“中断已经请求但仍显示完成”，首版固定以下不变量：

##### 13.1 单个 delta 的处理阶段

```text
provider raw chunk
    → normalized delta
    → MessageStreamWriter 串行提交
        ├─ event_seq 分配（仅内存）
        ├─ event + checkpoint 同一记录 fsync
        └─ committed
            ├─ 可选 live fanout（无订阅者则为空操作）
            └─ 交给 LangChain adapter yield AIMessageChunk
```

- raw chunk 尚未进入 writer，或 writer 在 `event + checkpoint` fsync 前失败：该 delta 不属于消息流；provider hook 抛错，不能先向 LangChain 或前端继续发这个 chunk。
- `event + checkpoint` 已 fsync：该 delta 已经是权威事实，即使进程在 fanout 或 `AIMessageChunk` yield 前退出，重连仍必须从 replay/snapshot 得到它。
- live fanout 没有客户端确认语义；队列丢失、连接关闭或客户端处理失败都只形成重连缺口，不回滚已提交事件。
- LangChain 聚合结果只服务 AgentLoop 最终业务处理，不反向覆盖消息流；如果聚合或后续 AgentLoop 在提交后失败，消息流保留已收到的半截 block，并以 `model.failed`/`stream.failed` 表达执行失败。
- append 或 fsync 抛错时不能假设磁盘一定没有该记录。writer 必须丢弃进程内缓存 checkpoint，重新扫描 JSONL，截断不完整尾部，并以最后一条完整记录重建 `event_seq`/幂等索引；该次提交不做 live fanout，后续提交不能复用可能已经写入的序号。

##### 13.2 stream 状态闸门

| 当前状态 | 允许的写入 | 必须拒绝或转换的写入 |
| --- | --- | --- |
| `open` | 模型、block、tool、interrupt 请求及正常终态 | 无条件迟到事件以当前线性顺序判断 |
| `interrupting` | 已开始事实的 `block.completed`、已有 `tool_call` 的 `tool_call.completed`、`model.completed/failed`、`tool.completed`，以及 `stream.interrupted/failed` | 新的 `model.started/retrying`、`block.started/delta`、新的 `tool_call.delta`、`tool.started` 和 `stream.completed` |
| `completed/interrupted/failed` | 读取 snapshot；新的中断请求转为 `interrupt.rejected` | 任何会改变业务事实的模型、block、tool 或 stream 事件 |

`interrupting` 仍允许已有调用的 `block.completed`、`tool_call.completed` 和 `tool.completed`，因为这些是已经开始事实的收尾；不允许创建新的 block、tool call 或 ToolExecution。工具事实无法确认时，必须提交 `tool.completed(status=completed, outcome=outcome_unknown)`（进程存活时）或由恢复扫描补出等价事实。重复中断不能覆盖首次 `interrupt_request_id`，而必须返回 `already_interrupting`。

进入 `interrupting` 后到达的工具结果只有在对应操作仍由当前 AgentLoop 持有且结果已在取消线性化点之前产生时才能提交；否则只记录取消/未知事实。释放 lease 后的迟到回调不能重新打开 ToolExecution、重启工具或改变终态。

##### 13.3 崩溃、取消和重连组合矩阵

| 发生位置 | 重启后的权威结果 | 前端展示 |
| --- | --- | --- |
| raw chunk/规范化尚未提交 | 不包含该 delta；当前执行若无安全续接则 `execution_lost` | 不补造文本，显示明确失败 |
| event/checkpoint 已提交、fanout 前 | 保留该 event_seq；replay/snapshot 补回 | 不能因为未实时收到而丢 block |
| `interrupt.requested` 已提交、事实终态前崩溃 | 恢复为 `stream.failed(execution_lost, after_interrupt_requested=true)`，不伪装成成功中断 | 显示执行丢失，并保留中断请求事实 |
| `stream.interrupted` 已提交 | 终态不可变；running block 以 `partial=true` 和中断原因闭合，未确认的 running tool 变为 `status=completed, outcome=outcome_unknown`，其持久资源 lease 按策略释放 | 显示用户中断，不显示工具成功或资源已销毁 |
| 无 `interrupt.requested` 的 `CancelledError` | `stream.failed(execution_cancelled)` 或明确的外部取消事实 | 不显示为用户主动打断 |
| SSE 断开或有界队列溢出 | 后端状态不变；按 `after_seq` replay，游标失效则 snapshot 替换 | 连接状态显示断开/恢复中，不能提前显示完成 |

客户端重连时必须先注册订阅再读取 replay，避免读取和订阅之间产生竞态；无法连续 replay 时以 `snapshot_seq` 替换整份 live projection，然后只应用大于 `snapshot_seq` 的新事件。终态事件或终态 snapshot 发出后服务端关闭 SSE，客户端将该 stream 标记为 terminal，不再无限重连。

##### 13.4 Provider carrier 归一化规则

不同 Provider 的 raw chunk 边界不能直接作为公共 block 边界，规范化器必须按语义而不是按网络包切分：

- 连续的同一 content `carrier_type` 且属于同一 ModelCall 的增量复用一个 `block_id`；content carrier 切换必须先提交旧 block 的 `block.completed`，再提交新 content block 的 `block.started`。如果切换到 `tool_calls[]`，只闭合当前 content block，然后开始 `tool_call.delta` 生命周期，不创建名为 tool call 的 MessageBlock。
- `reasoning_content`/`thinking` 使用 append；`reasoning_items` 按 provider item ID 做 upsert/patch。Provider 缺少 item ID 时必须生成稳定的本地 item key，并在同一 ModelCall 内保持不变，不能每个 chunk 新建 item。
- `redacted_thinking` 只生成状态/marker；签名、加密内容和 provider metadata 进入 provider state，不得被转换成可读 text delta。
- tool call 的名称和参数分片按同一 provider `tool_call_id` 合并；空名称、空参数或仅包含 finish reason 的 chunk 不能清空已经确认的字段，也不能新建调用。
- 空 chunk、usage chunk 和 finish-only chunk 可以推进 ModelCall/当前 block 的完成状态，但不得生成空的可见 `block.delta`。
- 规范化器必须为每个 carrier/block 记录来源顺序和局部序号；这使中断发生在任意 Provider chunk 之间时仍能按照同一 writer 闸门收尾。

#### 14. TurnExecutionScope、取消信号与资源租约分离

每个 `Turn` 在 AgentExecutionService 启动时创建一个进程内的 `TurnExecutionScope`，并以 `turn_stream_id` 注册到执行注册表。它是该 Turn 的执行边界、临时操作的资源所有者和持久资源 lease 的持有者，但不是 terminal、browser、MCP 连接或开发服务等持久资源的生命周期所有者；下一个 Turn 必须创建新的 scope，不能复用 Session 级或进程级取消状态。

`TurnExecutionScope` 不等同于一个万能 token，至少包含：

```text
TurnExecutionScope
├── cancellation_signal
├── deadline / timeout policy
├── model_call child scopes
├── tool_call child scopes
├── ephemeral cleanup registry
├── resource lease set
└── agent control inbox
```

`CancellationSignal` 只负责取消通知、取消原因、`is_cancelled`/`raise_if_cancelled()` 和取消 hook。Turn 标识、ModelCall 标识、AgentLoop 状态、消息流 checkpoint、资源 ID 和恢复事实不放入 signal，也不放入协议 payload。需要完整运行时语义的组件接收 `TurnExecutionScope`；只需要响应取消的组件按需接收 `CancellationSignal`。纯数据转换、事件序列化和普通查询不传递它们。

按需传递的边界固定为：

```text
TurnExecutionScope
  → AgentLoop
    → ProviderAdapter / delta hook
    → ToolExecutor
    → 长耗时子任务
```

不使用隐式的 Session 全局 token。外部中断 API 通过 `turn_stream_id` 从执行注册表找到 scope；AgentLoop 内部显式把 scope 或 signal 传给确实需要它的接口。应用或工作区关闭可以作为更外层的生命周期信号级联到 Turn scope，但它必须保留 `external_shutdown` 等原因，不能伪装成用户 `interrupt.requested`。

##### 14.1 持久资源由 ResourceManager 管理

`ResourceManager` 是 terminal、browser context、MCP connection、dev server 等跨 Turn 资源的生命周期权威。资源记录至少包含：

```text
ResourceRecord
├── resource_id
├── lifetime_scope = turn | session | workspace | global
├── created_by_turn_id
├── cleanup_policy
└── status

ResourceLease
├── resource_id
├── lease_id
├── turn_stream_id
├── operation_id
└── status
```

一次 Turn 内对持久资源的读写、导航、命令执行或查询，仍然创建一个可取消的 child operation scope，并持有对应 `ResourceLease`。取消或超时的边界是：

- Provider stream、临时 HTTP 请求、临时子进程和临时浏览器操作属于 Turn/ModelCall/ToolCall 的 ephemeral resource，child scope 结束时必须关闭。
- 持久资源的当前操作被取消时，必须中止该操作并释放 lease；资源本身默认继续运行，除非 `cleanup_policy` 明确要求随 lease 销毁，或用户/系统发出经过授权的显式 stop 命令。
- 资源 stop 不通过 `CancellationSignal` 表达，也不因前端断线隐式触发；它是 `ResourceManager` 的独立生命周期命令，并校验 `resource_id`、lease/所有权和当前状态。
- Turn 取消后，消息流只记录关联的资源操作停止或未知事实；不把 ResourceManager 的完整资源状态复制进 `message.v1`。snapshot 可以包含 `resource_refs` 和操作状态，资源详情仍由 ResourceManager 查询。

后端崩溃后，恢复扫描按 `resource_id` 和 lease 记录与实际资源做 reconcile。能够确认存活的资源进入 `recovered`，无法确认归属或状态的进入 `orphaned`/待人工或显式策略处理；不得因为一个 Turn 崩溃就盲目杀掉所有 terminal、browser 或 server 进程，也不得自动重放未知的资源操作。

##### 14.2 ModelCall 和 ToolCall 的子 scope

每个 ModelCall 和 ToolCall 可以从 Turn scope 派生 child scope：

- 用户中断、Turn 关闭或上层终止取消父 scope，并级联取消所有 child scope 和当前操作 lease。
- 单个 ModelCall 超时只关闭当前 Provider stream，允许 AgentLoop 根据策略产生 `model.failed`/`model.retrying`，不必自动取消整个 Turn。
- 单个 ToolCall 超时或失败只影响该工具执行；是否继续 Turn 由 AgentLoop 决定。若操作结果未知，必须保留 `outcome_unknown`，不得因 lease 释放而声称成功或回滚副作用。
- child scope 结束时必须释放它创建的 ephemeral 资源和 operation lease；释放 lease 不等价于销毁由 ResourceManager 管理的持久资源。

Provider 和工具的取消必须同时具备“信号通知”和“实际资源释放”两层：

```text
CancellationSignal.wait() / add_hook()
    → CancelableStream 取消 pending anext、关闭 raw stream
    → Tool abort handle / subprocess termination / HTTP close
    → release operation lease
    → AgentLoop observes cancellation
```

Provider wrapper 必须通过 `CancelableStream` 在 stream 正常结束、异常、signal 取消和消费者提前退出路径执行 close/finally 清理；读取器先取消 pending `anext()`，再等待 close 和读取任务收尾。`task.cancel()` 只作为外层资源释放超时或宿主强制关闭的兜底，不能替代 Turn 的语义取消。工具仍可通过 `add_hook()` 注册可验证的 abort；如果没有可验证的 abort 或结果查询能力，取消后仍必须把已经启动但未确认的执行恢复为 `outcome_unknown`，不能假设副作用已回滚。

前端断线只影响订阅，不触发 Turn scope 取消；后台任务没有前端连接时继续使用同一个 scope 和消息流 writer。后端崩溃后 scope 不可恢复，恢复流程必须根据持久化 `interrupt.requested`、checkpoint、resource lease 和执行事实重建状态，不能依赖内存 signal，也不发送必须在崩溃前产生的崩溃事件。

#### 15. AgentControlInbox 负责多次、带顺序的控制输入

`CancellationSignal` 是一次性的广播，不适合表达重复到达、需要排队、需要状态校验或需要返回结果的 AgentLoop 控制。每个 Turn scope 维护一个类型化的 `AgentControlInbox`，由唯一的 `AgentLoopControlCoordinator` 消费，并与消息流 writer、ResourceManager 和 child scopes 协作。

首版控制输入抽象为：

```text
interrupt
steer
approval.result
resume
resource.operation.result
```

每条控制命令至少包含 `command_id`、目标 `turn_stream_id`、调用方、幂等键和按 Turn 单调递增的 `control_seq`。同一命令重复投递只产生一次业务效果；控制命令不把 provider raw chunk、`AIMessageChunk` 或 `message.v1` 输出事件塞入 inbox。宿主进程 shutdown 属于更外层的生命周期信号，不伪装为普通 `steer` 或用户中断。

`AgentControlInbox` 本身是进程内队列，不是恢复的权威来源。协调器在入队前必须把命令意图、`control_seq`、状态（accepted/consumed/rejected）和幂等键写入 Turn 的控制检查点或独立控制日志；`interrupt` 的持久事实仍使用 `message.v1` 的 `interrupt.requested`。后端重启后只能重新校验尚未消费的控制命令：审批可以恢复到仍等待同一 ToolCall 的状态，steer/resume 只能作用于仍允许控制的 checkpoint，资源结果必须交给 ResourceManager reconcile；任何命令都不能只因为进程内 inbox 还留有内存副本就自动重放副作用。

控制命令的 accepted/consumed/rejected 是控制 API/控制日志语义，不自动扩展为一套新的消息流事件。只有会影响用户可见 Turn 状态的 interrupt 使用 `interrupt.requested`/`interrupt.rejected` 投影到 message.v1；steer、approval、resume 和资源操作结果继续通过控制状态与对应的 Activity/resource_refs 投影关联。

控制输入的边界为：

- 外部 API 先由 `AgentLoopControlCoordinator` 做身份、目标 Turn、状态和幂等校验，持久化可接受的控制意图后再按序放入 inbox；前端断线不是一条控制命令。
- `interrupt` 只有一条生效路径：协调器先在线性化队列中提交 `interrupt.requested`，提交成功后才触发 Turn 的 `CancellationSignal`。inbox 不得再绕过协调器直接调用第二个取消源。
- `steer` 只能在 AgentLoop 允许接收新指令的状态被消费；它改变后续模型输入或策略，不修改已经提交的 block delta，也不能让已中断或已终态的 stream 重新运行。
- `approval.result` 只能投递给仍处于等待审批的指定 ToolCall；Turn 取消会关闭等待并拒绝迟到的审批结果，不能在 `stream.interrupted` 后重新启动工具。
- `resume` 必须引用明确的 checkpoint/等待状态，并由 AgentLoop 决定是否继续；它不能把 `execution_lost` 的未知副作用自动重放成成功执行。
- `resource.operation.result` 必须交给 ResourceManager 校验 `resource_id`、`lease_id` 和 `operation_id`；资源状态更新不等于取消 signal，也不默认释放或销毁资源。

AgentLoop 使用单一协调循环观察规范化 provider delta、工具结果、AgentControlInbox 和取消信号：

```text
provider delta / tool result / control command / cancellation
                 ↓
      AgentLoopControlCoordinator
        ├── MessageStreamWriter：提交可见事实
        ├── Provider/Tool child scope：停止或继续操作
        └── ResourceManager：校验 lease 与资源生命周期
```

`open` 状态下控制命令按 `control_seq` 和消息流线性化顺序处理；进入 `interrupting` 后，新的 steer、resume、approval 和业务资源结果必须明确拒绝或转为清理确认，不能改变终态；进入 terminal 后只能读取状态或执行明确授权的资源管理命令。控制拒绝必须返回可诊断的 command 状态，不能静默丢弃，也不能伪造新的消息 delta。这样可以避免“取消信号、控制消息、消息事件和资源操作”各自拥有一套互相冲突的状态机。

#### 16. 非核心耗时路径采用通用 Activity 和可选 Handler

不把所有 AgentLoop 耗时状态都加入 `active_state.phase`。协议将状态拆成互相独立的维度：

```text
Activity
├── activity_id
├── kind
├── parent_activity_id?
├── scope_ref = turn | session | workspace | global
├── status = running | waiting | stopping | completed | failed | unknown
├── outcome = success | user_interrupt | provider_error
│             | execution_lost | outcome_unknown
├── summary?
├── cancellable
├── resumable
├── side_effect_policy
├── resource_refs[]
├── detail?
├── started_seq
├── last_event_seq
├── completed_seq?
├── started_at?
├── updated_at?
└── completed_at?
```

`scope_ref` 只表示 Activity 的公共归属，不携带 `TurnExecutionScope`、`CancellationSignal` 或其它运行时对象。`model_output`、`tool_call`、`tool_execution` 是协议内置的核心实体，继续使用已有的细粒度事件。上下文压缩、审批等待、子 Agent 等 Turn 内但不属于核心模型/工具生命周期的路径，统一登记为 Activity。Job 排队、Goal 续跑、后台任务、持久 terminal/browser/MCP 资源和 SSE 连接仍由各自的 Job、Goal、ResourceManager 或前端 transport 状态管理；TurnStream 只保存关联的 `activity`/`resource_refs` 投影。

上下文压缩是同一 Turn 内的边界活动：自动压缩发生在前一个 ModelCall 结束后、下一个 ModelCall 开始前，不创建新的 Turn、Job 或 TurnStream；压缩后的真实 Provider 请求分配新的 `model_call_id`，但仍沿用原 `turn_stream_id`。同一 Turn 连续触发多次压缩时，每次都必须分配新的 `activity_id`，并通过生命周期序号区分其先后。空闲状态下的手动压缩不挂靠活动 Turn，而作为 Session 级维护 Activity；下一次用户消息才创建新的 Turn。

每个 Activity 至少可以通过通用事件表达：

```text
activity.started
activity.updated(status=running | waiting | stopping)
activity.completed(outcome=success | user_interrupt | outcome_unknown)
activity.failed(outcome=provider_error | execution_lost | outcome_unknown)
```

通用事件必须经过同一个 `MessageStreamWriter`，与模型 delta、工具结果和中断事件共享 `event_seq`、checkpoint、终态闸门和恢复语义。默认 Handler 只保留 Activity 身份、状态、摘要、取消/恢复能力、外部副作用策略、资源引用、错误和最后更新时间；它可以丢弃阶段进度和 provider 私有细节，但不能丢失副作用边界、未知结果或恢复禁止事实。

语义 Handler 通过 `ActivityHandlerRegistry` 按 `kind` 注册，接口职责限制为：

```text
ActivityHandler
├── normalize/update：把内部事件转换为 Activity patch
├── snapshot_detail：向统一 stream.snapshot 提供可选的结构化 detail
├── recover：从 checkpoint 判断 recovered/unknown/orphaned
├── cancel/cleanup：提供可验证的停止和清理动作
└── capabilities：声明是否支持细粒度进度、取消、恢复和结果查询
```

Handler 只能计算扩展 detail 和状态 patch，不能绕过消息流 writer、直接修改 checkpoint 或单独产生前端事实。通用 Activity 投影和 Handler detail 必须在同一个持久化提交边界内写入。未注册 `kind`、Handler 版本不可用、detail 序列化失败或 Handler 恢复失败时，系统退回通用投影，并将 detail 标记为 unavailable；Handler 自身失败不影响主 TurnStream 继续提交。若 Handler 无法确认底层外部副作用的停止、结果或清理，则 Activity 必须进入 `outcome_unknown`/`execution_lost`，AgentLoop 不得因此伪造 `stream.completed`，这是底层安全事实约束而不是 detail 降级失败。

前端同样采用能力回退：先用 `active_state.kind` 和 Activity 通用状态显示“处理中/等待中/停止中/结果未知”等通用状态；存在匹配的 `detail schema` 和 Renderer 时，再展示统一 `stream.snapshot` 中的专用进度、审批、压缩或子 Agent 信息。未知 Activity kind 不得阻塞消息流，也不得被前端误判为模型文本或运行中的工具。

选择该分层而不是“所有路径都必须注册 Handler”，是为了让 AgentLoop 新增一条路径时立即拥有可持久化和可恢复的最小语义；选择该分层而不是“只保留一个通用状态”，是为了让审批、上下文压缩、子 Agent 和资源操作可以在需要时增加细粒度 snapshot，而不改变核心协议的状态枚举。

## Risks / Trade-offs

- [双写导致两条链路短暂不一致] → 两条链路共享同一规范化输入；增加 event sequence、终态对账和 deterministic stub 测试。
- [高频 delta 占用内存或拖慢前端] → 采用有界实时队列、短批/帧级渲染合并，snapshot 只返回当前投影而非无限原始事件。
- [断线期间发生事件缺口] → 以 `event_seq` 检测缺口，通过 snapshot 重建，再继续从游标消费；无法修复时显式报错。
- [中断竞态下出现迟到 delta] → stream 进入终止闸门后拒绝新的 delta，并把请求态与事实态分开记录。
- [后端崩溃后前端永久显示运行中] → 不等待崩溃事件；重连时以 snapshot/恢复扫描为权威，无法证明可继续时发布 `stream.failed(execution_lost)`。
- [先 fanout 后持久化导致重启回滚] → writer 在持久化 event/checkpoint 后才进入 live fanout；恢复扫描只承认已提交状态。
- [慢前端阻塞模型流] → live subscriber 使用有界队列；队列溢出时断开并要求 snapshot，不反压 LiteLLM raw stream。
- [首个 delta 后透明重试造成重复输出] → 首个语义 delta 提交后禁止同一 ModelCall 静默重试；后续重试必须新建 attempt 或发布失败终态。
- [Turn 取消误杀持久 terminal/browser/server] → 将临时操作清理与 ResourceManager 的资源生命周期、operation lease 和显式 stop 命令分离。
- [取消 signal、控制命令和消息事件产生双重状态机] → 由唯一 AgentLoopControlCoordinator 线性化控制输入；`CancellationSignal` 只广播一次取消，`message.v1` 只记录已提交事实。
- [迟到审批或资源回调重新启动已中断 Turn] → 以 `control_seq`、命令幂等键、ToolCall 状态和 resource lease 做状态门控；终态后的业务控制显式拒绝。
- [崩溃后持久资源被错误清理或重复操作] → ResourceManager 按 resource_id/lease reconcile，区分 recovered、orphaned 和 outcome_unknown，不依赖 Turn 内存 scope。
- [AgentLoop 校验重试前已发布最终文本] → ModelCall 的输出只标记为 intermediate/superseded，`stream.completed` 延迟到最终校验和 checkpoint 完成。
- [工具副作用已发生但结果未知] → 恢复为 `outcome_unknown`，默认不重放，并将该状态带入 snapshot 和失败终态。
- [旧事件路径绕过新串行器] → 新协议的 model/tool/stream 终态只由 writer 产生；旧 Trace 只接受已提交规范化 block 的诊断投影，聊天不再把 `TEXT_END`/`AGENT_END` 当作消息源。
- [协议字段过早固定] → 首版只固定关联键、生命周期、carrier type、delta 操作和终态；provider 扩展字段保持私有，复杂结构待真实场景验证。
- [未注册 Activity 丢失阶段细节] → 通用 Handler 至少保留身份、状态、错误、副作用边界、资源引用和恢复结论；需要细节的路径再注册带 schema/version 的语义 Handler，不允许以细节缺失掩盖未知副作用。

## Migration Plan

1. 先冻结本设计中的层级、事件信封、通用 Activity、Handler 回退、终态语义和首版传输约束，补充 `turn-message-stream` 规格与任务。
2. 将 LiteLLM raw stream 改为逐 chunk 规范化，新增 UpstreamDeltaHook、消息流 writer、AgentLoop 生命周期串行器、通用 Activity 默认 Handler/registry、独立订阅入口、snapshot 能力和后端重启恢复扫描；旧链路仅保留独立诊断能力。
3. Web 客户端直接接入新流，先实现 Activity 通用 Renderer，再按需接入 reasoning 首 delta、模型 attempt 重试、工具循环、工具未知结果、重连、缺口恢复和中断竞态的专用展示；协议错误通过连接状态和 snapshot 恢复显式呈现。
4. 删除旧实时 Trace 拼接到 Web 的适配、旧实时 feature flag 和双写专用兼容代码；旧协议仅保留仍被 Trace/历史等独立能力使用的部分。

回滚时关闭新流消费开关并停止新入口的客户端使用；Turn 历史、关联 Job 和旧 Trace 链路继续提供已有能力。新协议写入的状态不得破坏旧历史格式。

实现前必须提供故障注入验收：raw delta 规范化前后、event/checkpoint 提交前后、fanout 前后、ModelCall 校验重试前、tool started/completed 之间、interrupt requested 与 stream completed 并发时，均能通过重启后的 snapshot 得到确定且不重复的结果。
