# litellm-aimessage-content-adapter Specification

## Purpose
将 LiteLLM 返回的 assistant 内容以单一、不可变且有序的 `AIMessage.content` block 序列保存，并在恢复模型上下文时按照来源 provider、目标 provider 和能力配置生成临时请求投影。工具调用继续遵守 LangGraph 的标准消息契约。
## Requirements
### Requirement: assistant 内容必须使用有序、可校验的 content blocks

LiteLLM 适配器 SHALL 将文本、reasoning、thinking 和 redacted thinking 保存在经过共享 Schema 校验的有序 `AIMessage.content` block 序列中，不得生成项目自定义的 `litellm_payload` 包装块。

`AIMessage.content` SHALL 保留跨 provider 字段的原始顺序。LiteLLM 的独立字段必须使用以下 carrier block 保存，不能继续作为 `AIMessage` 顶层独立字段，也不能合并成统一的 `type: "reasoning"`：

```json
{
  "type": "reasoning_content",
  "reasoning_content": "先确认工作区结构。"
}
```

```json
{
  "type": "reasoning_items",
  "reasoning_items": [
    {
      "type": "reasoning",
      "id": "rs_item_002",
      "status": "completed",
      "summary": [{"type": "summary_text", "text": "检查范围已确定。"}],
      "encrypted_content": "..."
    }
  ]
}
```

Responses reasoning item SHALL 作为 `reasoning_items` carrier 的数组元素保存，并整体保留 LiteLLM 返回的 item。适配器不得按 `id`、`status`、`content`、`summary`、`encrypted_content` 或项目当前已知字段建立白名单；provider 后续增加的字段也必须随 item 一起保存：

```json
{
  "type": "reasoning",
  "id": "rs_item_002",
  "status": "completed",
  "content": [{"type": "reasoning_text", "text": "已确定检查范围。"}],
  "summary": [{"type": "summary_text", "text": "检查范围已确定。"}],
  "encrypted_content": "..."
}
```

Chat Completions 的纯字符串 `reasoning_content` SHALL 转换为一个 `reasoning_content` carrier；LiteLLM 已返回的 `thinking_blocks` SHALL 整体追加其 `thinking` 或 `redacted_thinking` block，不得抽取 `thinking`/`text` 字段后重建；可见模型文本 SHALL 使用 `text` block。缺失、`null` 或空值不生成空 content block。

适配器 SHALL NOT 将 reasoning、thinking 或 encrypted 数据的副本写入最终 `AIMessage.additional_kwargs` 或项目自定义 `extras`。流式阶段可以暂时使用 `additional_kwargs` 和 `part_id/index` 组装，但 finalizer 必须清理这些运行时字段。`invalid_tool_calls` SHALL 按 LangChain `AIMessage` 字段原样保留，空数组也必须随 canonical 消息持久化，以保证消息序列化和反序列化结构稳定。

#### Scenario: 纯文本 assistant 响应

- **WHEN** LiteLLM 返回 `content="完成"`，且没有 reasoning 或 thinking 字段
- **THEN** `AIMessage.content` 为 `[{'type': 'text', 'text': '完成'}]` 或等价可恢复文本，additional kwargs 不包含 reasoning 备份

#### Scenario: reasoning 与最终文本同时返回

- **WHEN** LiteLLM 返回 `content="最终回答"` 和 `reasoning_content="先检查文件"`
- **THEN** `AIMessage.content` 包含有序的 `reasoning_content` carrier 和 text block，不存在 `litellm_payload` 或顶层 reasoning 备份字段

#### Scenario: Responses reasoning item 与最终文本同时返回

- **WHEN** Responses 返回带 summary/encrypted_content 的 reasoning item 和最终文本
- **THEN** LiteLLM 返回的完整 item 深复制到 `reasoning_items` carrier，provider 字段不嵌套在 `extras.response_item`，未被项目识别的 provider 字段也不得丢失，carrier 与最终文本 block 的相对顺序保持

#### Scenario: reasoning 字段与正文交错

- **WHEN** 流式响应依次返回 `reasoning_content`、Responses reasoning item 和正文 delta
- **THEN** 最终 `AIMessage.content` 按返回顺序包含 `reasoning_content` carrier、`reasoning_items` carrier 和 text block，不能按字段类型重新排序

#### Scenario: LiteLLM block 含有项目未知字段

- **WHEN** LiteLLM 的 reasoning、thinking 或 redacted_thinking block 含有当前项目没有定义的 provider 扩展字段
- **THEN** 适配器整体复制该 block；只有流式合并所生成的临时 `part_*`、`index` 和 `extras` 在 canonical finalizer 中清理，Schema 不得丢弃未知 provider 字段

#### Scenario: 空的 output item added

- **WHEN** Responses 先发送没有正文的 `response.output_item.added`
- **THEN** 流式适配器不提交空 reasoning/message block；后续有正文的 delta 或 done item 才形成 carrier/provider content block

### Requirement: 工具调用必须遵守 LangGraph 消息契约

适配器 SHALL 将可执行工具调用保存到 `AIMessage.tool_calls`，将工具结果保存为带有对应 `tool_call_id` 的 `ToolMessage`。工具调用和结果不得仅存在于普通 content block 中。

#### Scenario: assistant 同时返回文本和工具调用

- **WHEN** LiteLLM 返回文本、reasoning 和一个可执行工具调用
- **THEN** 文本/reasoning 进入 `AIMessage.content`，工具名称、参数和调用 id 进入 `AIMessage.tool_calls`，LangGraph 可以正常路由工具

#### Scenario: 工具返回大型结果

- **WHEN** 工具返回大型 JSON 结果
- **THEN** 结果作为 `ToolMessage` 按 rollout 消息规则保存，`tool_call_id` 与 assistant 调用 id 一致，assistant content 不复制结果正文

### Requirement: 历史恢复必须执行目标 provider 投影

系统 SHALL 将持久化 `AIMessage` 视为不可变来源，在模型请求边界根据来源 provider、目标 provider 和能力配置生成临时投影。投影至少支持独立控制可见 content、`reasoning_content`、`thinking_blocks`、reasoning item、summary 和 encrypted reasoning。

投影 SHALL 保留用户消息、可见 assistant 文本、标准工具调用和工具结果；目标 provider 不支持的 reasoning 必须过滤，不得改写 checkpoint、rollout JSONL 或源消息。

#### Scenario: 从 reasoning provider 切换到普通模型

- **WHEN** 当前目标模型不接受 reasoning_content、thinking_blocks 和 reasoning item
- **THEN** 请求历史只携带可见文本和工具契约，原始思考仍保留在源 `AIMessage.content`

#### Scenario: 同一 provider 回放 encrypted reasoning

- **WHEN** 来源 provider 与目标 provider 相同，且目标能力允许 encrypted replay
- **THEN** 投影可以携带 encrypted 内容，但必须删除新的请求不能复用的 server-owned id、status、session 和 response 生命周期字段

#### Scenario: 跨 provider 只保留安全摘要

- **WHEN** 目标 provider 接受 summary 但不接受来源 encrypted payload
- **THEN** 投影只保留可安全转换的 summary，删除 encrypted 内容和来源专属字段

#### Scenario: 空 reasoning item 不参与请求历史

- **WHEN** 源 content 只有没有正文、summary 或 encrypted 内容的 reasoning item
- **THEN** 投影过滤该 item，不把 provider server id 当成可执行历史

### Requirement: 投影不能修改 checkpoint 来源消息

历史投影 SHALL 返回新的临时消息或请求 payload，不得就地修改 `AIMessage.content`、`AIMessage.tool_calls`、`AIMessage.response_metadata`、SQLite 行或 rollout JSONL。相同 checkpoint 在不同目标 provider 下重复读取，必须得到相同的源消息和确定性的目标投影。

#### Scenario: 同一历史切换两个模型

- **WHEN** 先用不支持 encrypted 的模型读取，再用支持回放的来源模型读取同一 checkpoint
- **THEN** 第一次读取不会清除第二次所需的 source content，第二次仍可按能力生成回放请求

### Requirement: 流式响应只在最终提交时形成 canonical AIMessage

流式 chunk 可以暂时携带 provider 原始片段、运行时 `part_id/index` 或额外字段，但 assistant 稳定完成时 SHALL 合并为一条经过 Schema 校验的有序 content block `AIMessage`。最终持久化消息不得因每个 chunk 重复保存 reasoning 或工具参数。

#### Scenario: 多个流式 reasoning/text/tool chunk

- **WHEN** 模型连续返回 reasoning、文本和工具参数 chunk
- **THEN** 内存合并结果只生成一条最终 `AIMessage`，content 中是有序 carrier/provider blocks，tool_calls 只保存一次规范化结果

#### Scenario: assistant 流式过程中崩溃

- **WHEN** 进程在最终 AIMessage 提交前崩溃
- **THEN** 未完成 chunk 可以丢失，但此前已提交消息保持可读，不产生多条互相矛盾的 assistant revision

### Requirement: checkpoint、rollout 和 Web 投影必须识别有序 content 布局

checkpoint saver、上下文恢复器、rollout message projection 和 Web 历史读取 SHALL 从经过 Schema 校验的有序 content blocks 提取可见文本、reasoning summary、reasoning 文本、encrypted 存在标记和工具阶段，不得依赖 `additional_kwargs` 的 reasoning 备份或额外 payload 文件。

Web projection SHALL 不返回 encrypted 正文；full checkpoint 恢复 SHALL 保留完整有序 `AIMessage.content`，模型请求投影才按 provider 能力过滤。

#### Scenario: checkpoint roundtrip

- **WHEN** saver 将含 reasoning、summary、encrypted 和 tool_calls 的 AIMessage 写入后再读取
- **THEN** 恢复的 AIMessage content 顺序、carrier 原始字段、工具调用和来源元数据可重建，不需要额外 kwargs 备份

#### Scenario: 默认 Web 历史

- **WHEN** Web 请求 user、tool_summary、final_response 历史
- **THEN** SQLite/reader 从 reasoning carrier 和 provider block 得到安全摘要和存在标记，不解析或返回 encrypted 正文

#### Scenario: 工具详情和模型 full history 分别读取

- **WHEN** 用户请求 bounded tool_call/tool_result，或 LangGraph 请求完整可执行历史
- **THEN** 前者只读取所需消息内容，后者读取完整源消息并在目标 provider adapter 中投影；两者都不回退到 payload 文件

