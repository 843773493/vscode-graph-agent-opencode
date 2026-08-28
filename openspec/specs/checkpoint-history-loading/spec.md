# checkpoint-history-loading Specification

## Purpose
为会话列表、聊天时间线和诊断读取提供统一的有界历史加载能力，直接使用 SQLite context view 和 rollout JSONL offset 支持从头、从尾、游标中间和中心位置读取多轮对话。
## Requirements
### Requirement: Web 历史以 rollout JSONL 和 SQLite 为唯一来源

系统 SHALL 直接从会话 rollout 的 `index.sqlite` 和 `rollout.jsonl` 读取 Web 历史摘要、详情、Turn 边界和 cursor 定位。`TraceEventStore`、`logs/traces/messages.jsonl`、`logs/traces/events.jsonl` 和 `turn_history/turns/*.json` 不得作为 `/bootstrap` 或 `/history` 的数据源，也不得在 rollout 缺失时静默回退。

#### Scenario: rollout 中存在工具调用和结果

- **WHEN** 测试数据通过 `RolloutCheckpointSaver` 写入 rollout，并包含用户消息、带 tool_calls 的 assistant 消息、tool 消息和最终 assistant 响应
- **THEN** 历史 API 从 SQLite view/range 和 rollout JSONL 组装完整 Turn，Trace 文件是否存在不影响结果

#### Scenario: 旧 Trace 文件不能伪造历史

- **WHEN** 会话只有旧 Trace 或 TurnHistory 数据而没有有效 rollout 或 SQLite
- **THEN** 历史 API 返回明确的 rollout 数据缺失/损坏错误，不返回旧投影中的 Turn

### Requirement: 历史加载支持多种方向和不透明 cursor

系统 SHALL 支持从最早位置、从最新位置、cursor 之前、cursor 之后以及以 cursor 为中心加载历史。cursor MUST 绑定 rollout、view、branch、projection epoch 和逻辑位置。

#### Scenario: 从头加载

- **WHEN** 调用方请求从历史起点加载若干 Turn
- **THEN** 系统根据 SQLite view range 返回最早的完整 Turn，并提供继续向后的 cursor 或明确表示没有更多数据

#### Scenario: 从尾加载

- **WHEN** 调用方请求最新若干 Turn
- **THEN** 系统通过 active view 和 turns 索引返回最新 Turn，不读取更早完整 checkpoint

#### Scenario: 从中间向前加载

- **WHEN** 调用方携带历史 cursor 请求 cursor 之前的 Turn
- **THEN** 系统返回 cursor 之前的完整 Turn，不重复、不拆分 anchor Turn

#### Scenario: 中心展开

- **WHEN** 调用方请求以某个历史位置为中心加载前后范围
- **THEN** 系统返回锚点两侧完整 Turn，并分别返回两侧继续加载状态

#### Scenario: 游标前加载一个 Turn

- **WHEN** 调用方请求 cursor 之前的 1 个 Turn
- **THEN** 系统只返回 cursor 前最近的完整 Turn，不返回 cursor 所在 Turn 或更早 Turn，并提供继续向前 cursor

#### Scenario: 游标上下各加载一个 Turn

- **WHEN** 调用方请求以 cursor 为中心向前和向后各加载 1 个 Turn
- **THEN** 系统返回锚点两侧各 1 个完整 Turn，不重复锚点或拆分 Turn，并分别提供两侧状态

#### Scenario: 游标前加载五个 Turn

- **WHEN** 调用方请求 cursor 之前的 5 个 Turn
- **THEN** 系统按历史顺序返回最多 5 个完整 Turn，并在预算耗尽时返回有界结果和继续 cursor

### Requirement: Turn 是默认分页边界

系统 SHALL 以完整 Turn 作为默认加载和分页单位。用户消息、合并 steering Job、工具活动和最终状态 MUST 保持在同一个 Turn 中；内部消息和工具记录不得把一个 Turn 拆成独立分页项。

#### Scenario: 合并 steering 消息

- **WHEN** 多个 steering 消息被一次 Job 合并执行
- **THEN** 历史返回一个完整执行 Turn，并保留源消息和合并 Job 的身份信息

#### Scenario: 工具调用中的 Turn

- **WHEN** 一个 Turn 包含多个工具调用和工具结果
- **THEN** 默认 summary 返回一个 Turn，工具数量和状态作为 Turn 内摘要

### Requirement: 加载内容可以按 include 策略选择

系统 SHALL 支持独立选择用户消息、可见 text、工具摘要、工具调用、工具结果、reasoning 摘要/详情、encrypted reasoning 元数据、内部消息和 metadata。`assistant_text` 不作为 canonical 事件类型；如果 API 为已有调用返回该字段，它只能是统一 response part 的派生别名。默认工具展示 MUST 只包含工具名称和状态，完整参数和结果必须显式请求。

#### Scenario: 默认工具摘要

- **WHEN** 调用方未请求工具详情
- **THEN** 系统从 SQLite tool_calls 返回工具名称、状态和有界错误摘要，不读取工具正文

#### Scenario: 请求完整工具结果

- **WHEN** 调用方明确请求工具结果并且通过服务端大小限制
- **THEN** 系统根据 SQLite message offset 读取对应 JSONL，并在预算耗尽时返回 bounded detail cursor

#### Scenario: 隐藏内部消息

- **WHEN** 调用方未请求内部消息
- **THEN** 系统只返回公开可见内容，visibility 为 internal 的消息不会被误当成用户消息

#### Scenario: 加载工具摘要和模型最终响应

- **WHEN** 调用方选择用户消息、tool_summary 和模型最终响应
- **THEN** 每个完整 Turn 返回用户消息、工具名称与状态摘要和最终 assistant 响应，不返回工具参数或完整结果

#### Scenario: 加载工具调用、结果和模型最终响应

- **WHEN** 调用方选择用户消息、tool_call、tool_result 和模型最终响应
- **THEN** 每个完整 Turn 返回用户消息、工具调用、对应工具结果和最终 assistant 响应，并遵守详情大小限制

### Requirement: 历史与 live 使用同一 Turn response part 模型

系统 SHALL 为历史 summary、历史 detail 和 live 流式事件提供同一套有序 response part 语义模型。part 的来源坐标必须能够表达 `AIMessage.content` 块、`reasoning_items` 内部项、同一 assistant 的 `tool_calls.call_index`、产生结果的 `assistant_message_sequence` 和后续 `ToolMessage.tool_call_id`；不得要求历史事件拥有 live 流式 delta 才能渲染。live 尚未提交到 rollout 时可以只携带稳定 `part_id`，不得伪造 JSONL message sequence 或新增全局 part index。

#### Scenario: 历史 summary 只缺少投影细节

- **WHEN** 首次加载返回用户消息、reasoning summary、tool summary 和最终文本
- **THEN** 前端将其转换为与 live 相同的 response part 类型，缺失的中间文本和工具正文只标记为未加载，不生成伪造的 delta 事件

#### Scenario: summary include 不泄漏未请求的中间部件

- **WHEN** summary 请求只包含 `user`、`reasoning_summary`、`tool_summary` 和 `final_response`
- **THEN** 返回 reasoning summary、工具名称/状态和最终文本；普通 reasoning、encrypted reasoning、tool_call 参数和 tool_result 正文均不得被 response-part adapter 直接返回

#### Scenario: live 工具缺少真实调用 ID

- **WHEN** live 工具事件只有流式 `part_id` 而没有真实 `tool_call_id`
- **THEN** 前端使用稳定 `part_id` 关联本次调用和结果，但不得把 `part_id` 序列化为 LangChain `tool_call_id`

#### Scenario: 历史 detail 补齐同一 Turn

- **WHEN** 用户展开 Turn 详情
- **THEN** Saver 通过 SQLite source 坐标和命中的 rollout JSONL 消息返回完整有序 response parts，前端替换该 Turn 的 summary projection，用户看到的排序与 live 一致

#### Scenario: 历史不回退到旧消息字段渲染

- **WHEN** 历史 Turn 返回 `response_parts`，但没有 live SSE events
- **THEN** 前端只将 `response_parts` 转换为 TimelineItem 并交给统一 ResponsePart renderer，不再从 `assistant_text`、`thinking_blocks`、`tool_summary` 或旧 trace 字段拼出第二套消息
- **AND** summary 缺少的中间内容保持为未加载状态，只有用户请求 detail 后才通过同一 response-part 模型补齐

#### Scenario: tool_call 位于 AIMessage content 之后

- **WHEN** assistant content 同时含有 reasoning/text 且 tool_calls 非空
- **THEN** 渲染顺序为 content parts、tool-call parts、匹配的 tool-result parts 和后续 assistant parts；不得用新的全局 part index 改写 canonical 顺序

### Requirement: Gateway 默认历史加载采用渐进阶段

系统 SHALL 在会话所属 Gateway 应用默认渐进策略。首次加载 SHALL 返回最新 1 个 Turn 的 user、可展示 reasoning_summary、tool_summary 和 final_response；后续默认按 4、16、64 个 Turn 向前加载，且只返回 user 和 final_response。

#### Scenario: 首次进入会话

- **WHEN** Web 首次打开一个会话
- **THEN** Gateway 根据 active view 只返回最新 1 个完整 Turn，工具摘要默认折叠

#### Scenario: 到达顶部后继续滚动加载四个 Turn

- **WHEN** 用户到达顶部并在 pending 状态中继续向上滚动
- **THEN** 前端请求未加载位置之前的 4 个完整 Turn，并保持原有可见 Turn 的滚动锚点

#### Scenario: 继续滚动加载十六个和六十四个 Turn

- **WHEN** 用户完成上一批加载后再次到达顶部并继续滚动
- **THEN** Gateway 依次返回 16 个、64 个完整 Turn，旧批次不包含 tool_summary、tool_call 或 tool_result

#### Scenario: 渐进阶段结束后继续加载

- **WHEN** 客户端已使用最后一个渐进批次并继续向前加载
- **THEN** 系统重复最后阶段的批次大小，直到历史起点或明确没有更多历史

#### Scenario: 顶部 pending 避免重复请求

- **WHEN** 用户首次触达顶部且仍有更早历史
- **THEN** 前端先进入短暂 pending 状态，不立即发起下一批；后续继续滚动才触发请求，单阶段请求完成或失败前不得重复发起

#### Scenario: 当前 Turn 立即加载工具详情

- **WHEN** 用户在当前 Turn 工具菜单中启用详情
- **THEN** 前端立即重新加载当前 Turn 的 tool_call 和 tool_result，其它 Turn、会话和 Gateway 默认策略不变，工具区域仍默认折叠

#### Scenario: 配置由会话所属 Gateway 决定

- **WHEN** Gateway A 通过 Gateway B 加载存储在 B 上的会话
- **THEN** B 使用自己的 Gateway 配置决定投影和批次，A 不得覆盖 B 的策略

### Requirement: 所有历史响应有服务端硬上限

系统 SHALL 对 Turn 数、消息数、JSONL record 数、响应字节数、单项字符数和详情批次设置硬上限。客户端传入更大参数不得绕过限制。

#### Scenario: 请求超过响应预算

- **WHEN** 请求范围超过服务端字节或字符预算
- **THEN** 系统返回已完成的有界结果、继续 cursor 和明确 truncated 状态

### Requirement: 普通追加不使历史 cursor 失效

系统 SHALL 允许普通追加、continue/resume 和 checkpoint 提交继续使用已有 cursor。rewind、replay 造成的 branch 切换、删除、回滚或 view 重建必须推进 projection epoch 并使旧 cursor 返回明确 stale 错误。

#### Scenario: 普通追加后的旧 cursor

- **WHEN** 客户端读取历史期间会话追加了新的 Turn
- **THEN** 客户端仍可使用原 cursor 读取原 view 锚点之前的历史

#### Scenario: Rewind 后的旧 cursor

- **WHEN** 会话执行 rewind 并切换 active branch/view
- **THEN** 使用旧 projection epoch 的 cursor 返回明确 stale 错误，不静默返回新分支第一页

### Requirement: 历史读取使用单次 snapshot 和批量 I/O

系统 SHALL 为一次历史请求创建单个 read snapshot，执行页面级 SQLite 查询，并按 JSONL offset 分组读取命中的行。系统 MUST NOT 为每个 Turn 重复初始化、查询 offset 或打开文件。

#### Scenario: 128 Turn 页面加载

- **WHEN** 请求 4、16 或 64 个历史 Turn
- **THEN** 请求只校验一次 read 水位，批量获取全部 offset，不重放完整 checkpoint 或扫描未命中的 JSONL 前缀

### Requirement: 所有 rollout 数据访问使用唯一 RolloutCheckpointSaver

系统 SHALL 通过 `RolloutCheckpointSaver` 作为 checkpoint、Web history、fork context 和 Turn 状态的唯一业务层入口。Saver MUST 在内部使用 `RolloutContextReader` 解析 SQLite view/range，并提供 projection、detail、full 三种模式；业务层不得直接依赖 `RolloutStorage`、`RolloutAppendWriter` 或 `RolloutContextReader`。

#### Scenario: 默认 Web projection

- **WHEN** Web 请求最新 Turn 或向前加载历史摘要
- **THEN** RolloutCheckpointSaver 使用内部 reader 的 projection 模式，不调用 full materialize，不构造完整 checkpoint messages 列表

#### Scenario: 复杂 view 仍使用同一入口

- **WHEN** active view 包含多级 rewind、replay 或 compaction range
- **THEN** Saver 仍使用同一内部 reader 和 SQLite context view 解析流程，不切换到旧 Trace 或 rollout 前缀扫描

#### Scenario: LangGraph 恢复 full

- **WHEN** LangGraph saver 或 context fork 需要可执行消息列表
- **THEN** Saver 显式使用内部 reader 的 full 模式，根据 SQLite range 顺序返回 BaseMessage

#### Scenario: 非法 view 所有模式统一失败

- **WHEN** SQLite view range 缺失、越界或成环
- **THEN** projection、detail 和 full 都返回明确 context view 错误，不返回部分结果

### Requirement: 历史读取性能可验收

系统 SHALL 提供包含 128 Turn、普通 reasoning、encrypted reasoning、tool_call/tool_result 和 finalization 的确定性 fixture。真实 8011 链路 MUST 测量 bootstrap、4/16/64 向前加载和当前 Turn 详情重载，并记录 p50/p95；热路径 p95 目标为 100ms 内，稳定浏览器 prepend/锚点恢复 p95 目标为 150ms 内，首次冷 prepend 预算为 500ms 内。

#### Scenario: 混合 128 Turn 读路径

- **WHEN** 测试通过 8011 → Gateway → workspace backend 请求 bootstrap、4/16/64 向前加载和当前 Turn 详情
- **THEN** 每个请求使用一个 snapshot 和页面级批量读取，记录 p50/p95，并证明没有 materialize 全量 checkpoint

### Requirement: 前端 Turn 定位不暴露 View 实现

系统 SHALL 让前端历史时间线和从历史位置发起的 fork/rewind/replay 只使用 `turn_id` 作为稳定定位键；`view_id`、branch lineage、checkpoint 和 message range 只作为后端内部字段或诊断信息返回。

#### Scenario: 同一个 Turn 出现在多个 view

- **WHEN** 一个 Turn 因 append、rewind 或 compaction 出现在多个不可变 view 中
- **THEN** 前端仍只显示一个稳定的 `turn_id`，后端根据当前 active head 和 cursor epoch 解析本次请求实际使用的 view
- **AND** 前端无需维护一个 Turn 到多个 view 的映射

