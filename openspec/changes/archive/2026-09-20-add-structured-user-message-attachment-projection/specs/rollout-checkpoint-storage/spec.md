## MODIFIED Requirements

### Requirement: canonical carrier 与工具调用顺序必须可无损恢复

系统 SHALL 原样保存由 `UserContentBuilder` 生成的用户 `HumanMessage.content` 和 assistant `AIMessage.content` 的有序 block 序列。用户 content 中的文本、附件 manifest、provider rich block、其 canonical block metadata 及模型实际使用的预览 data URL SHALL 按原始 block 顺序保存；assistant content 中的 `reasoning_content`、`reasoning_items`、`thinking`、`redacted_thinking` 和 `text` carrier 及其数组顺序也 SHALL 原样保存。`AIMessage.tool_calls` SHALL 作为同一 assistant 消息 content 之后的调用序列保存；`ToolMessage` SHALL 通过 `tool_call_id` 与调用关联。SQLite 投影 SHALL 使用 `(message_sequence, content_block_index, item_index)` 定位用户和 assistant content/reasoning，使用 `(assistant_message_sequence, call_index)` 定位工具调用，并在工具结果 part 上保留产生它的 `assistant_message_sequence` 关系坐标；不创建与这两套坐标重复的全局 part 序号。

#### Scenario: 一个 assistant 同时包含多种 content carrier 和工具调用

- **WHEN** `AIMessage.content` 依次包含 reasoning、summary、thinking、redacted thinking 和两段 text，且 `tool_calls` 包含两个调用
- **THEN** rollout JSONL 保留原始 content 数组和两个 tool_calls，SQLite 能按 content block/item 坐标恢复前五个 content part，并按 call_index 恢复两个工具调用；恢复顺序为 content parts → tool calls → 后续 ToolMessage

#### Scenario: 工具卡片合并不改变 canonical 顺序

- **WHEN** 一个 assistant 的 tool_call 与后续 tool message 通过相同 `tool_call_id` 关联
- **THEN** Web detail 可以把它们投影为一个工具卡片，但 LangGraph full 恢复仍返回独立的 assistant 和 tool 消息，且不得把 tool_call 插入 `AIMessage.content`

#### Scenario: 一个 human 同时包含文本、附件和预览

- **WHEN** `HumanMessage.content` 依次包含文本、相对附件路径 manifest 和图片预览 `image_url` data URL
- **THEN** rollout JSONL 与 checkpoint view 保留三个用户 block 的顺序和预览数据，SQLite 只保存用于定位、附件身份和有界投影所需的信息，不把预览正文复制到独立索引字段
