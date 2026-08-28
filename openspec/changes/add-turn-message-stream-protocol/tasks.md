本清单按实现依赖排序，而不是按设计章节编号排序：协议/持久化/Provider 是基础，AgentLoop 与运行时控制随后接入，最后完成 API、Web、迁移和验证；第 9、10 组对应设计 E 的详细运行时策略。

## 1. 协议模型与生成边界

- [x] 1.1 新增 `boxteam.workspace.message.v1` protobuf 定义，覆盖事件信封、关联键、event_seq、stream/model/block/tool 生命周期、interrupt、snapshot、failure 和 recovery 字段
- [x] 1.2 为 reasoning_content、reasoning_items、thinking、redacted_thinking、text 以及工具调用定义稳定的 carrier/delta 表示，明确敏感字段不进入前端 projection
- [x] 1.3 生成 Python、TypeScript 和 JSON 边界代码，补充协议 round-trip、未知字段和枚举终态测试
- [x] 1.4 在消息流协议目录和新增源码目录补齐 `AGENTS.md`，说明可修改与不可修改边界
- [x] 1.5 在协议 schema 中补齐 `MessageStreamEvent`、Turn、Job、TurnStream 关联键、`status/outcome/completion_reason` 字段边界、`partial`、`tool_call.completed` 的 incomplete/cancelled 状态、`tool.completed(status=completed, outcome=outcome_unknown)`、`model.failed(outcome=user_interrupt, retryable=false)`、Activity 通用生命周期、可选 Handler detail、统一 snapshot 的完整恢复字段和 `active_state.kind/phase` 联合结构，并覆盖 JSON/Protobuf round-trip
- [x] 1.6 在 snapshot schema 中增加 ModelCall、MessageBlock、ToolCall、ToolExecution 和 Activity 的 `started_seq`、`last_event_seq`、可选 `completed_seq` 及展示时间字段，明确其与 `snapshot_seq`、事件日志 `event_seq` 的关系，并补充 JSON/Protobuf round-trip

## 2. 消息流持久化与串行提交

- [x] 2.1 实现按 `turn_stream_id` 单写入的事件提交器，保证 event_seq 分配、终态闸门、重复请求幂等和迟到 delta 拒绝
- [x] 2.2 实现事件日志与 checkpoint 的统一提交边界，确保事件持久化成功前不发生 live fanout，并能从 checkpoint 重建 blocks、ModelCall、ToolExecution、AgentLoop 和 interrupt projection
- [x] 2.3 实现有界 live subscriber 队列与 `after_seq` replay，订阅者断开或队列溢出时不反压 LiteLLM 上游
- [x] 2.4 实现 snapshot 查询与缺口恢复：可连续 replay 时按序补发，无法补齐时先返回 snapshot 再发送 snapshot_seq 之后的事件
- [x] 2.6 固化 `stream.snapshot` 的序号不变量：envelope event_seq 等于 snapshot_seq，snapshot 不额外消耗 event_seq；重连先注册订阅再读取 snapshot/replay；旧 snapshot 丢弃，并发 live event 暂存后按高水位和连续序号应用，不制造伪缺口或重复事件
- [x] 2.7 扩展统一 snapshot 投影以独立恢复 partial block、incomplete/cancelled tool call、`outcome_unknown` ToolExecution、ModelCall/AgentLoop、通用 `activities[]`、`active_state`、interrupt、resource_refs 和 failure/recovery 状态；按 snapshot_seq 记录当前活动实体高水位
- [x] 2.5 为每个提交点增加故障注入测试，覆盖 event_seq 分配前后、event/checkpoint 提交前后和 fanout 前后，并验证重启后的结果不重复且可解释
- [x] 2.8 在 checkpoint 投影中维护实体生命周期序号和时间字段，确保同一 Turn 内连续的 `context.compaction` 使用不同 Activity 身份并在 snapshot 中同时保留，不依赖数组顺序合并实体

## 3. LiteLLM raw delta 规范化

- [x] 3.1 将 LiteLLM 同步和异步流改为逐 chunk 消费，不再先收集完整 raw stream 后才生成 AIMessageChunk
- [x] 3.2 实现统一的 NormalizedModelDelta，按 carrier 顺序维护 block_id/block_index/local_seq，并保留 reasoning_items 的结构化 patch 与 redacted 状态
- [x] 3.3 在规范化 delta 与 LangChain AIMessageChunk 转换之间接入消息流提交 hook，固定 raw chunk → NormalizedModelDelta → event/checkpoint commit → fanout → AIMessageChunk → LangChain 聚合顺序，使消息流和最终 AIMessage 使用同一份规范化输入；hook/提交失败时禁止下游继续聚合
- [x] 3.4 保留最终 AIMessage 的 carrier 顺序、tool_calls 独立字段和 provider state，同时禁止最终聚合结果反向充当实时消息源
- [x] 3.5 为首个 reasoning/text delta、carrier 切换、结构化 reasoning、tool call delta、上游异常和异步取消补充 provider 单元测试
- [x] 3.6 固化不同 Provider 的 carrier/block 归一化规则：carrier 切换先闭合旧 block，空 metadata/usage/finish chunk 不产生可见 delta，缺失 reasoning item ID 时生成稳定本地 key，tool_call 参数始终按 provider tool_call_id 合并

## 4. AgentLoop、工具和终态协调

- [x] 4.1 为每次真实 ModelCall 建立独立 model_call_id/attempt，并将 model.started/completed/failed/retrying 统一提交到消息流
- [x] 4.2 将 AgentLoop 的最终业务校验放在 stream.completed 之前，标记中间或取代的 ModelCall 输出，禁止重试前发布最终完成事件
- [x] 4.3 将模型 tool_calls、tool.started、tool.completed 拆成独立生命周期，崩溃或取消后将未确认结果恢复为 outcome_unknown
- [x] 4.4 实现 interrupt.requested、interrupt.rejected、stream.interrupted 和 stream.failed 的统一线性化，确保中断请求持久化后才发出取消信号并释放当前 operation lease（TurnExecutionScope、ResourceManager 和 AgentControlInbox 由第 9 节补齐；通用 Activity 由第 10 节补齐）
- [x] 4.5 为 interrupt 与 block.delta、stream.completed 并发、工具执行中断、ModelCall 校验重试、控制消息竞态和 CancelledError/异常路径补充 AgentLoop 集成测试
- [x] 4.6 实现后端重启恢复扫描：识别运行中且无法安全续接的 Turn，提交 execution_lost、after_interrupt_requested 和 resumable=false 的权威状态；`interrupt.requested` 已持久化但停止事实未提交时必须恢复为 `stream.failed`，不能伪造 `stream.interrupted`
- [x] 4.7 为任意中断点补齐显式收尾：partial block 的 `block.completed`、半截/未启动 tool call 的 `tool_call.completed`、已启动工具的实时 `tool.completed(status=completed, outcome=outcome_unknown)`，以及 `model.failed(outcome=user_interrupt, retryable=false)`

## 5. 工作区 API 与 SSE 订阅

- [x] 5.1 新增独立的会话消息流订阅入口，复用现有 Gateway 代理和 SSE 传输，支持 turn_stream_id、after_seq、snapshot 请求及请求 ID 透传
- [x] 5.2 新增消息流历史/snapshot 查询入口，确保无前端连接的后台任务仍能被后续客户端完整恢复
- [x] 5.3 将用户中断 API 接入消息流的 interrupt.requested 提交流程，返回请求 ID 和当前权威状态，而不是直接发布旧 session_interrupted
- [x] 5.4 为订阅断开、游标过旧、未知 stream、已终态中断和后端恢复失败返回显式错误及可诊断的 request_id
- [x] 5.5 为 API 订阅、游标 replay、snapshot 替换和中断竞态编写 FastAPI/SSE 集成测试

## 6. Web 消息流状态与展示

- [x] 6.1 新增按 turn_stream_id 管理的消息流 reducer/store，按 event_seq 串行应用并对 event_id、block_id、tool_execution_id 做幂等去重
- [x] 6.2 实现 SSE 连接、断线重连、游标确认、缺口检测、snapshot 请求和连接状态展示，不让连接断开伪装成终态
- [x] 6.3 将 carrier block 投影为 Thinking、Redacted Thinking、文本和工具时间线，支持浏览器帧/短批渲染但不丢协议事件
- [x] 6.4 在 snapshot 应用时完整替换 AgentLoop、当前 attempt、blocks、ToolExecution、interrupt 和 failure 状态，并与权威 Turn detail 对齐
- [x] 6.5 覆盖首个 reasoning delta、文本与 reasoning 切换、模型重试、工具未知结果、中断和 execution_lost 的 Web 状态测试
- [x] 6.6 修改 `src/clients/web` 后运行 `bun run --cwd src/clients/web build`，并修复静态检查、类型检查和构建错误
- [x] 6.7 前端区分 partial block、incomplete/cancelled tool call 和 `outcome_unknown` ToolExecution，确保 `stream.interrupted` 后不存在仍显示 running 的悬挂投影
- [x] 6.8 实现 snapshot reducer 的高水位和竞态处理：按 snapshot_seq 丢弃旧 snapshot，暂存并按序应用并发 live event，依据 active_state.kind/phase 和 Activity 通用状态更新阶段指示器，terminal snapshot 停止重连，running snapshot 继续订阅
- [x] 6.9 前端按实体生命周期序号和 `event_seq` 恢复跨 ModelCall、压缩 Activity 与 Block 的时序；禁止使用 `activities[]`/`model_calls[]`/`blocks[]` 数组位置或 `updated_at` 推断全局顺序

## 7. 迁移切换与兼容代码清理

- [x] 7.1 让旧 Trace/执行事件和新消息流在迁移阶段共享同一规范化 delta 输入，并用显式诊断记录两条链路的终态差异
- [x] 7.2 将 Web 实时回答默认切换为消息流，验证历史读取继续使用权威 Turn detail，旧协议仅保留给仍需要它的 Trace/历史能力
- [x] 7.3 删除旧 TEXT_DELTA/TEXT_END/AGENT_END 到 Web 实时消息的适配、旧实时拼接器、双写专用 feature flag 和兼容分支，不保留静默回退路径
- [x] 7.4 清理废弃的 part/旧事件语义、重复的 LiteLLM 聚合发送路径和未使用的前端 reducer，并用全仓检索确认没有实时兼容引用
- [x] 7.5 更新相关测试、开发配置和 API 文档，明确新协议是 Web 实时展示的唯一来源

## 8. 全链路验证与边界审查

- [x] 8.1 使用 `uv` 运行后端静态分析、单元测试和集成测试，使用 `bun` 运行 Web 静态分析、类型检查、构建和测试（本变更相关源码 ruff、聚焦后端 97 项测试、恢复/中断 9 项测试、Web 49 项测试、tsc 和生产构建均通过；全仓 pytest 的 11 个失败属于既有无关基线问题，全仓 ruff 仍包含既有/生成代码问题）
- [x] 8.2 通过完整 Gateway → 工作区后端 → Web 链路验证健康检查、消息流订阅、request_id 透传、重连 snapshot 和用户中断
- [x] 8.3 运行故障注入矩阵：任意 delta 边界、模型校验重试、tool started/completed 之间、interrupt 与 completed 竞态、fanout 前后和进程重启
- [x] 8.4 从前端以真实用户操作审查断线、重复点击中断、快速发送新 Turn、工具结果未知、刷新页面和后端重启等边界，记录产物到 `out/tests/temp/turn-message-stream-review/`
- [x] 8.5 复用同一审查任务持续复测发现的问题并修复，至少运行四小时后确认不存在新的可复现消息顺序、终态或展示一致性问题（审查报告：`out/tests/temp/turn-message-stream-review/artifacts/596-final-real-user-ux-audit-report.md`）
- [x] 8.6 增加四类 delta 级别打断故障注入：reasoning/text block 中断、tool_call 参数中断、ToolExecution 已启动且结果未知、中断与模型完成竞态；验证 live 事件、checkpoint、snapshot 和前端 reducer 的终态一致
- [x] 8.7 增加四阶段 snapshot hydration 测试：reasoning、text、tool_call、tool_execution；验证 active_state、完整数组投影、阶段切换和中断/终态 snapshot 的前端展示一致
- [x] 8.8 增加连续两次上下文压缩的 snapshot/replay 故障注入测试，覆盖第一次完成、第二次运行中、snapshot 高水位切换和压缩事件与后续 ModelCall/Block 的生命周期序号关系

## 9. TurnExecutionScope 与资源治理

- [x] 9.1 为每个 Turn 创建独立的 `TurnExecutionScope`，按 `turn_stream_id` 注册和销毁；不得复用 Session 级或进程级取消状态，且不向 `message.v1` payload 暴露运行时 signal
- [x] 9.2 实现职责单一的 `CancellationSignal`：支持取消原因、状态检查、取消 hook 和父子 scope 级联；将 deadline、ModelCall/ToolCall 子 scope、ephemeral cleanup registry 和资源 lease set 放在 `TurnExecutionScope`，不把它们塞进万能 token
- [x] 9.3 只在 AgentLoop、Provider、工具和长耗时外部操作等边界按需传递 scope/signal；为 ModelCall 和 ToolCall 支持局部超时，局部超时不得伪造用户中断或自动取消整个 Turn
- [x] 9.4 为异步 Provider 接入统一 `CancelableStream` 读取器：在 `anext()` 与 Turn `CancellationSignal.wait()` 之间竞速，取消时主动取消读取任务、等待 raw stream `aclose()`/`close()` 和读取任务收尾；正常结束、异常、消费者提前退出都执行幂等清理，`task.cancel()` 仅作为资源释放超时后的强制兜底
- [x] 9.5 为运行中的 ToolCall 注册 child scope 和 abort/cleanup 句柄，取消时关闭当前 HTTP、临时子进程或浏览器操作；无法确认结果时统一恢复为 `outcome_unknown`，不得自动重放副作用工具
- [x] 9.6 将 terminal、browser context、MCP connection、development server 等持久资源迁移到 `ResourceManager`，实现 `ResourceRecord`、`ResourceLease`、生命周期范围和 cleanup policy；Turn 取消只停止操作并释放 lease，默认不销毁资源
- [x] 9.7 实现资源 stop 与 Turn cancellation 的独立入口，校验 `resource_id`、lease/所有权和操作状态；补充崩溃后的 resource/lease reconcile、`recovered`/`orphaned`/未知操作状态，不盲目杀进程或重放资源操作
- [x] 9.8 实现按 Turn 注册的 `AgentControlInbox` 和唯一 `AgentLoopControlCoordinator`，支持命令 ID、幂等键、`control_seq`、状态门控以及 interrupt、steer、approval.result、resume、resource.operation.result 的明确接受/拒绝结果；Inbox 不作为恢复权威
- [x] 9.9 将 provider delta、工具结果、控制命令和取消信号接入同一 AgentLoop 协调循环；确保 interrupt 只有“持久化 interrupt.requested → 触发 CancellationSignal”一条路径，迟到审批、steer、resume 和资源结果不能复活已中断 Turn
- [x] 9.10 持久化控制意图及 accepted/consumed/rejected 状态，覆盖 inbox 入队与消费之间崩溃后的恢复校验；禁止依赖内存 inbox 或自动重放可能产生副作用的工具/资源操作
- [x] 9.11 补充跨 ModelCall/ToolCall 传播、跨 Turn 隔离、ModelCall 局部超时、Provider 连接关闭、工具取消、持久资源在 Turn 中断后仍存活、显式资源 stop、资源 lease 崩溃恢复、控制消息竞态、前端断线不取消后台 Turn 和后端崩溃后依赖 checkpoint 恢复的集成测试

## 10. 通用 Activity 与可选语义 Handler

- [x] 10.1 定义 `Activity` 通用模型，至少覆盖 `activity_id`、kind、父子关系、scope、running/waiting/stopping/completed/failed/unknown 状态、outcome、摘要、取消/恢复能力、副作用策略、resource_refs 和更新时间
- [x] 10.2 增加 `activity.started`、`activity.updated`、`activity.completed`、`activity.failed` 通用事件，并接入现有 TurnStream writer、event_seq、checkpoint、终态闸门和 snapshot
- [x] 10.3 实现未注册 Activity kind 的默认 Handler：保留最小安全事实，允许丢弃阶段进度和 Provider 私有细节；取消或崩溃时不得伪造成功或自动重放未知副作用
- [x] 10.4 实现 `ActivityHandlerRegistry` 与 Handler 能力声明，支持结构化 detail、细粒度进度、统一 snapshot 内的 projection detail、取消/清理和恢复判断；Handler 不得绕过消息流 writer 直接写前端事实
- [x] 10.5 为 Handler 缺失、版本不兼容、恢复失败和 detail 不可用提供通用回退：Handler/detail 失败不影响主 TurnStream；若无法确认底层副作用，则保留 Activity 的 `unknown/execution_lost` 和 resumable 状态，禁止虚假 `stream.completed` 或自动重放
- [x] 10.6 将上下文压缩、审批等待、子 Agent 和资源操作等可细化路径接入 Handler；Job 队列、Goal、后台任务、持久资源和 SSE 连接继续由所属生命周期管理，不强行纳入 Turn Activity
- [x] 10.7 为 Web 增加 Activity 通用 Renderer 和按 kind/detail schema 注册的专用 Renderer；未知 kind 必须显示通用状态，不得误显示为模型文本或运行中的工具
- [x] 10.8 增加通用 Activity 的 live/replay/snapshot、取消、Handler 细节、Handler 缺失、后端崩溃、外部副作用未知和前端通用回退测试
