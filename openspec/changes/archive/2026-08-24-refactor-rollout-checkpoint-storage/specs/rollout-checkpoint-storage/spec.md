## Purpose

为长时间运行的 Agent 会话提供一个只追加的可读 rollout JSONL 和一个可快速重建上下文的 SQLite 权威数据库，避免 checkpoint 重复保存完整 messages 数组。

## ADDED Requirements

### Requirement: Rollout JSONL 只保存不可变消息

系统 SHALL 为每个 rollout 保存一个 UTF-8 `rollout.jsonl`。每行 SHALL 是一条不可变 canonical message，消息 role 只能是 `user`、`assistant` 或 `tool`，并包含稳定的 sequence、具体 message_id 和 turn_id。JSONL 不得保存 checkpoint、branch、rewind、fork、compaction 或其它控制记录。

#### Scenario: 追加一轮对话

- **WHEN** Agent 写入用户消息、assistant 输出、工具调用和工具结果
- **THEN** 系统按消息顺序向同一个 `rollout.jsonl` 追加对应的 user/assistant/tool 消息，不重复写入此前完整 messages 数组

#### Scenario: assistant 包含文本和工具调用

- **WHEN** provider 返回同时包含文本、reasoning block 和 tool_calls 的 AIMessage
- **THEN** rollout 保存一条 role 为 `assistant` 的 canonical message，LangGraph 恢复时仍返回原始结构

#### Scenario: 控制语义不进入 JSONL

- **WHEN** 系统执行 checkpoint、rewind、compaction、replay 或 fork
- **THEN** rollout.jsonl 不追加控制记录，相关状态只写入 SQLite

### Requirement: 消息追加不得通过流式重复造成 O(n²)

系统 SHALL 在稳定消息完成后追加一次完整 canonical message。流式 assistant 和逐步组装的工具参数在完成前不得每个 chunk 追加一份完整消息；未提交的流式内容可以在崩溃时丢失。

#### Scenario: 流式 assistant 完成

- **WHEN** 一个大型 assistant 消息经历多个流式 chunk 后完成
- **THEN** rollout 只追加一条最终 canonical assistant message，而不是为每个 chunk 追加完整正文

#### Scenario: 流式过程中崩溃

- **WHEN** 进程在 assistant 消息完成前崩溃
- **THEN** 系统可以丢弃未提交的流式内容，但此前已提交的消息和 SQLite 控制状态不得被删除

### Requirement: SQLite 是上下文和版本的权威数据库

系统 SHALL 使用单个 `index.sqlite` 保存数据库版本、消息 offset、Turn、工具投影、reasoning 投影、checkpoint、branch、context view、fork 来源、retention 和提交状态。SQLite 不得被定义为可以仅从 JSONL 重建完整控制状态的缓存。

#### Scenario: SQLite schema 迁移

- **WHEN** 软件升级需要改变 SQLite 表结构
- **THEN** 系统通过 `schema_migrations` 在事务中执行有序 migration，并更新 `database_meta.schema_version`

#### Scenario: SQLite 损坏

- **WHEN** `PRAGMA integrity_check` 或读取关键控制表失败
- **THEN** 系统进入 `recovery_required` 并要求从 SQLite 备份恢复，不得仅扫描 JSONL 伪造 branch、checkpoint 或 context view

#### Scenario: 核心字段集中定义

- **WHEN** 业务代码需要写入或读取 checkpoint、view、branch 或 commit
- **THEN** 操作通过统一数据库层和集中 schema 参数执行，不得在业务模块散落表名和 SQL 参数

### Requirement: JSONL 与 SQLite 提交边界一致

系统 SHALL 先将消息写入并 fsync 到 `rollout.jsonl`，再在 SQLite 事务中提交 messages、projection、control、view、checkpoint、全部 `checkpoint_channels` 行和 `database_meta`。`put_writes` 可以针对已提交 checkpoint 使用独立 SQLite 事务追加 pending writes。SQLite 提交的 JSONL offset 必须已经可靠写入文件。

#### Scenario: 未提交 JSONL 尾部

- **WHEN** 进程在 JSONL 写入后、SQLite 提交前崩溃
- **THEN** 启动恢复根据 `database_meta.committed_jsonl_offset` 忽略或截断未提交尾部，并保留此前已提交状态

#### Scenario: SQLite 提交后恢复

- **WHEN** SQLite 事务已经提交但进程随后崩溃
- **THEN** 系统根据已 fsync 的 JSONL 和 SQLite 提交记录恢复该批消息，不得回退已提交 checkpoint

### Requirement: SQLite 能按 offset 快速定位消息

系统 SHALL 在 `messages` 保存 message_sequence、turn_id、role、jsonl_offset、jsonl_length、content_hash 和 commit_id，并为 Turn、role、offset 和 checkpoint view 建立索引。读取单个 Turn 或窗口时不得从 JSONL 文件头线性扫描到目标。

#### Scenario: 读取中间 Turn

- **WHEN** 调用方请求游标附近的 Turn
- **THEN** 系统先通过 SQLite 找到逻辑消息范围，再只读取命中的 JSONL offset/length

#### Scenario: 读取大型工具结果摘要

- **WHEN** 调用方只请求工具名称和状态
- **THEN** 系统从 SQLite tool_calls 投影返回摘要，不打开大型 tool result JSONL 正文

### Requirement: Context view 通过 SQLite 范围表达

系统 SHALL 使用不可变 `context_views` 和 `context_view_ranges` 表表达有效消息上下文。view 可以引用其它 view 的逻辑范围或直接引用 JSONL 消息范围，但不得复制完整 messages 数组。`context_view_jumps` SHALL 支持长 view 链的快速跳转和 cycle 检查。

#### Scenario: rewind 到历史前缀

- **WHEN** 用户将 active head rewind 到历史 checkpoint 的某条消息
- **THEN** 系统创建新 branch 和新 view，引用目标 view 的有效前缀并隐藏旧后缀，不修改旧消息

#### Scenario: compaction 保留摘要和后缀

- **WHEN** compaction 生成摘要并保留最近消息
- **THEN** 系统追加摘要消息，并创建由摘要范围和保留消息范围组成的新 view，不复制旧消息正文

#### Scenario: view 引用非法

- **WHEN** view range 指向不存在的 view、越界消息、错误 branch 或形成循环
- **THEN** projection、detail 和 full 读取都返回明确的 context view 错误，不返回部分历史

### Requirement: SQLite 保存完整 LangGraph checkpoint envelope 和逐 channel 状态

系统 SHALL 在 `checkpoints` 保存 checkpoint_id、namespace、parent checkpoint、branch、view、commit、checkpoint version、timestamp、metadata、`versions_seen` 和 `pending_sends`。系统 SHALL 在 `checkpoint_channels` 为 `channel_values`、`channel_versions` 和 `updated_channels` 的并集保存每个 channel 一行。

`messages` channel 的行 SHALL 使用 rollout view 指针，不保存完整 messages 数组；其它 channel SHALL 使用写入时 serializer 保存序列化值、值状态、channel version、长度和 hash。checkpoint metadata、channel BLOB、`versions_seen` 和 `pending_sends` 都不得重复保存完整 messages 数组。

#### Scenario: 恢复最新 checkpoint 的全部 channel

- **WHEN** LangGraph 请求一个 thread 的最新 checkpoint，且 checkpoint 同时包含 messages、计数器、任务状态和 provider 状态 channel
- **THEN** saver 从 SQLite 恢复 checkpoint envelope、逐 channel version、`updated_channels`、所有非 messages channel 和 pending writes，并通过 messages channel 的 view 指针 materialize 可执行的 LangChain BaseMessage 列表

#### Scenario: messages channel 使用 checkpoint view

- **WHEN** `checkpoint_channels` 中存在 `channel_name=messages` 的 channel 行
- **THEN** 该行的 `context_view_id` 必须等于 `checkpoints.view_id`，恢复时只从该 view 读取消息，不读取当前 active view，也不从 checkpoint BLOB 读取完整 messages 数组

#### Scenario: 区分缺失 channel 和 None channel

- **WHEN** 一个非 messages channel 没有出现在 `channel_values`，或明确保存了 Python `None`
- **THEN** 前者以 `value_state=absent` 恢复为缺失 channel，后者以 `value_state=present` 解码为 None，不得把两种状态混淆

#### Scenario: 未变化的非 messages channel 仍属于当前快照

- **WHEN** 新 checkpoint 中某个非 messages channel 的值与 parent checkpoint 相同
- **THEN** 当前 checkpoint 仍保存自己的 `checkpoint_channels` 行和值，恢复时不隐式读取 parent checkpoint 的 channel，也不因值未变化而返回旧 checkpoint 的对象

#### Scenario: 恢复历史 checkpoint

- **WHEN** 调用方请求已经被后续消息覆盖的历史 checkpoint
- **THEN** 系统使用该 checkpoint 固定的 view 和对应 channel rows，而不是当前 active view 或最新 channel value

#### Scenario: checkpoint namespace 隔离

- **WHEN** 同一 thread 在多个 `checkpoint_ns` 下保存 checkpoint，并分别调用最新、按 ID 和列表查询
- **THEN** 每次查询都必须把请求 namespace 作为 checkpoint 条件；不同 namespace 的 checkpoint、parent、channel 和 pending writes 不得互相命中
- **AND** namespace 不存在时返回该 namespace 的空结果或明确缺失错误，不得借用另一个 namespace 的 active head

#### Scenario: 恢复 checkpoint-level 状态

- **WHEN** checkpoint 包含 `versions_seen` 或 `pending_sends`
- **THEN** `get_tuple` 使用 checkpoint 保存的 serializer、长度和 hash 恢复原始结构；serializer 缺失或 hash 不匹配时返回明确 checkpoint 数据错误

### Requirement: pending writes 可恢复

系统 SHALL 在 SQLite 保存 LangGraph pending writes 的 checkpoint、task_id、task_path、channel、写入顺序、serializer、值 BLOB、长度和 hash，并保持同步/异步 saver 的 parent checkpoint 契约。

#### Scenario: 恢复 pending writes

- **WHEN** checkpoint 存在尚未合并的 pending writes
- **THEN** `get_tuple` 和对应异步接口按 task path、task_id、channel 和 write_index 返回与 LangGraph 契约一致且顺序稳定的 pending writes

### Requirement: Saver 内部读取入口区分 projection/detail/full

系统 SHALL 由 `RolloutCheckpointSaver` 提供唯一业务层读取入口，并在内部使用 `RolloutContextReader`。`projection` 只读取 SQLite 投影和命中的小型 JSONL 消息，`detail` 读取指定 Turn 的 bounded JSONL 内容，`full` 才 materialize 完整 BaseMessage 列表。业务调用方不得直接依赖 Reader 或组合低层 SQLite 和 JSONL primitive。

#### Scenario: Web summary 不 materialize full

- **WHEN** Web 请求默认 Turn summary 或历史渐进加载
- **THEN** Saver 通过内部 reader 只执行 projection，不构造完整 checkpoint messages 列表

#### Scenario: 工具详情 bounded 读取

- **WHEN** 用户请求当前 Turn 的 tool_call 和 tool_result
- **THEN** Saver 通过内部 reader 只读取该 Turn 命中的 JSONL 行，并执行单项、Turn 和总响应预算

#### Scenario: LangGraph full 恢复

- **WHEN** checkpoint saver 需要交给 LangGraph 可执行的消息列表
- **THEN** reader 显式使用 full 模式，按照 SQLite view ranges 返回正确顺序的 BaseMessage

### Requirement: final response、tool summary 和 reasoning 由 SQLite 投影定位

系统 SHALL 在 SQLite 的 turns、message_projections、tool_calls 和 reasoning_blocks 中保存 final response 指针、工具摘要、reasoning 摘要和 encrypted reasoning 元数据。最终响应优先由 SQLite finalization 指针确定，provider phase 标记次之，旧数据才允许 heuristic fallback。

#### Scenario: 中间 assistant 与最终响应并存

- **WHEN** 一个 Turn 包含 tool_call、中间 assistant 文本和最终 assistant 文本
- **THEN** SQLite turns.final_message_sequence 指向的消息才作为 final_response，其余可见 assistant 文本属于 assistant_text

#### Scenario: encrypted reasoning 默认读取

- **WHEN** assistant 消息包含 provider encrypted reasoning
- **THEN** Web 默认只返回 SQLite 中的安全摘要和存在标记，不返回或解密 encrypted payload

### Requirement: canonical carrier 与工具调用顺序必须可无损恢复

系统 SHALL 原样保存 assistant `AIMessage.content` 中的 `reasoning_content`、`reasoning_items`、`thinking`、`redacted_thinking` 和 `text` carrier 及其数组顺序。`AIMessage.tool_calls` SHALL 作为同一 assistant 消息 content 之后的调用序列保存；`ToolMessage` SHALL 通过 `tool_call_id` 与调用关联。SQLite 投影 SHALL 使用 `(message_sequence, content_block_index, item_index)` 定位 content/reasoning，使用 `(assistant_message_sequence, call_index)` 定位工具调用，并在工具结果 part 上保留产生它的 `assistant_message_sequence` 关系坐标；不创建与这两套坐标重复的全局 part 序号。

#### Scenario: 一个 assistant 同时包含多种 content carrier 和工具调用

- **WHEN** `AIMessage.content` 依次包含 reasoning、summary、thinking、redacted thinking 和两段 text，且 `tool_calls` 包含两个调用
- **THEN** rollout JSONL 保留原始 content 数组和两个 tool_calls，SQLite 能按 content block/item 坐标恢复前五个 content part，并按 call_index 恢复两个工具调用；恢复顺序为 content parts → tool calls → 后续 ToolMessage

#### Scenario: 工具卡片合并不改变 canonical 顺序

- **WHEN** 一个 assistant 的 tool_call 与后续 tool message 通过相同 `tool_call_id` 关联
- **THEN** Web detail 可以把它们投影为一个工具卡片，但 LangGraph full 恢复仍返回独立的 assistant 和 tool 消息，且不得把 tool_call 插入 `AIMessage.content`

### Requirement: 历史和 live 必须共享语义响应模型

系统 SHALL 让历史 summary、历史 detail 和 live 流式事件都适配到同一个有序 `TurnResponsePart` 语义模型。历史 summary 可以省略中间事件，历史 detail 必须能通过 SQLite source 坐标恢复这些事件；前端不得为历史和 live 实现两套独立的排序或渲染模型。

#### Scenario: 历史 summary 与 live 使用同一渲染器

- **WHEN** live 流式事件包含 text/reasoning 增量，历史首次加载只包含 final text、tool summary 和 reasoning summary
- **THEN** 两者都能转换为带 `part_id`、`source`、`projection` 和 `status` 的统一 response part，前端使用同一个 renderer；历史 summary 缺失的中间 part 不得被伪造为 live delta

#### Scenario: 历史工具详情展开

- **WHEN** 用户点击某个 Turn 的工具详情
- **THEN** 前端通过 Saver detail API 获取该 Turn 的 tool_call、ToolMessage 和中间 response parts，并用 detail projection 替换 summary projection，不直接读取 JSONL 或拼接伪事件

### Requirement: 跨 provider 恢复必须按能力过滤 reasoning

系统 SHALL 在恢复 LangChain messages 时保留原始 assistant 消息的可见文本、tool_calls 和最终响应；encrypted reasoning 只有在目标 provider 与来源 provider 匹配且目标 provider 声明支持回放时才可进入请求 payload。跨 provider 切换时必须丢弃无法识别的 reasoning payload，不得因此丢失其它消息或破坏消息顺序。

#### Scenario: 从 reasoning provider 切换到不识别 encrypted reasoning 的 provider

- **WHEN** 历史 assistant 消息包含来源 provider 的 encrypted reasoning，当前模型使用另一个不支持该 payload 的 provider
- **THEN** LangChain 请求只保留用户消息、可见 assistant 文本、tool_call/tool_result 和最终响应；encrypted reasoning 被过滤，历史 checkpoint 与 JSONL 不被修改

#### Scenario: 切回原 provider

- **WHEN** 当前 provider 与 encrypted reasoning 的来源 provider 相同，并声明 `reasoning_content_replay` 或 Responses reasoning 回放能力
- **THEN** provider adapter 可以按原消息顺序回放该 reasoning payload，且不会把服务端 response item 的 id/state 原样带回下一次请求

### Requirement: 大型正文只保存在 JSONL

系统 SHALL 将大型 assistant 消息、工具参数、工具结果和 encrypted reasoning 正文完整保存在 JSONL。SQLite 只能保存类型、状态、长度、哈希、有界投影和 offset/length，不得创建 payload_ref、外置消息正文或 `rollout/payloads/`。

#### Scenario: 默认工具摘要

- **WHEN** 调用方只请求 tool_summary
- **THEN** 系统只访问 SQLite tool_calls，不解析大型工具参数和结果正文

#### Scenario: 显式读取大型正文

- **WHEN** 调用方明确请求大型 tool_call 或 tool_result
- **THEN** 系统通过 SQLite offset 读取 JSONL，并在超过预算时返回明确的 truncated 状态
