## MODIFIED Requirements

### Requirement: assistant 内容必须使用有序、可校验的 content blocks

LiteLLM 适配器 SHALL 将文本、reasoning、thinking 和 redacted thinking 先转换为经过共享 Schema 校验的 normalized content parts/item drafts。v2 canonical 路径必须由 `assistant_output`/`reasoning` semantic item 和稳定 content-part identity 表达，`AIMessage` 只是 LangChain 执行或 checkpoint projection，不是 v2 rollout 的事实来源。旧 v1 message 的 `AIMessage.content` carrier 只允许由一次性 `legacy_import_v1_to_v2` migration staging 读取，不能作为正常 provider/history/checkpoint/runtime 的兼容路径。

`AIMessage.content` 在兼容投影中 SHALL 保留跨 provider 字段的原始顺序。LiteLLM 的独立字段必须使用以下 carrier block 保存，不能继续作为 `AIMessage` 顶层独立字段，也不能合并成统一的 `type: "reasoning"`：

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

Responses reasoning item SHALL 作为 `reasoning_items` carrier 的数组元素保存，并整体保留 LiteLLM 返回的 item。适配器不得按 `id`、`status`、`content`、`summary`、`encrypted_content` 或项目当前已知字段建立白名单；provider 后续增加的字段也必须随 item 一起保存。v2 item finalizer 可以从同一 carrier 生成 canonical reasoning item/content part，但不得丢失 source provider/type/version 或未知字段。

Chat Completions 的纯字符串 `reasoning_content` SHALL 转换为一个 `reasoning_content` carrier；LiteLLM 已返回的 `thinking_blocks` SHALL 整体追加其 `thinking` 或 `redacted_thinking` block，不得抽取 `thinking`/`text` 字段后重建；可见模型文本 SHALL 使用 `text` block。缺失、`null` 或空值不生成空 content block。

适配器 SHALL NOT 将 reasoning、thinking 或 encrypted 数据的副本写入最终 `AIMessage.additional_kwargs` 或项目自定义 `extras`。流式阶段可以暂时使用 `additional_kwargs` 和 `part_id/index` 组装，但 finalizer 必须清理这些运行时字段。`invalid_tool_calls` SHALL 按 LangChain `AIMessage` 字段原样保留，空数组也必须随兼容 projection 持久化；v2 canonical item 通过明确的 payload/extension 字段表达同一事实。

#### Scenario: 纯文本 assistant 响应

- **WHEN** LiteLLM 返回 `content="完成"`，且没有 reasoning 或 thinking 字段
- **THEN** normalized output 生成一个带稳定 identity 的 text content part；一次性 migration staging 如需保留 v1 projection 才生成 `[{'type': 'text', 'text': '完成'}]`，v2 canonical semantic kind 为 `assistant_output`，additional kwargs 不包含 reasoning 备份

#### Scenario: reasoning 与最终文本同时返回

- **WHEN** LiteLLM 返回 `content="最终回答"` 和 `reasoning_content="先检查文件"`
- **THEN** normalized parts 按来源顺序保留 reasoning carrier 和 text part；一次性 migration staging 可以生成 v1 AIMessage 投影，v2 形成 assistant output/reasoning item 或其 content parts，不存在 `litellm_payload` 或顶层 reasoning 备份字段

#### Scenario: Responses reasoning item 与最终文本同时返回

- **WHEN** Responses 返回带 summary/encrypted_content 的 reasoning item 和最终文本
- **THEN** LiteLLM 返回的完整 item 深复制到 reasoning carrier，provider 字段不嵌套在 `extras.response_item`，未被项目识别的 provider 字段也不得丢失，carrier 与最终文本 part 的相对顺序保持

#### Scenario: reasoning 字段与正文交错

- **WHEN** 流式响应依次返回 `reasoning_content`、Responses reasoning item 和正文 delta
- **THEN** finalizer 按返回顺序生成 carrier/content part 与 canonical item draft，不能按字段类型重新排序

#### Scenario: LiteLLM block 含有项目未知字段

- **WHEN** LiteLLM 的 reasoning、thinking 或 redacted_thinking block 含有当前项目没有定义的 provider 扩展字段
- **THEN** 适配器整体复制该 block；只有流式合并所生成的临时 `part_*`、`index` 和 `extras` 在 finalizer 中清理，Schema 不得丢弃未知 provider 字段

#### Scenario: 空的 output item added

- **WHEN** Responses 先发送没有正文的 `response.output_item.added`
- **THEN** 流式适配器不提交空 reasoning/message block；后续有正文的 delta 或 done item 才形成 carrier、content part 或 canonical item

### Requirement: 工具调用必须遵守 LangGraph 消息契约

适配器 SHALL 将可执行工具调用归一化为独立的 canonical `tool_call` item，将工具结果归一化为带对应 `tool_call_id` 的 `tool_result` item。v2 LangChain projection（以及一次性 migration staging 的必要输出）可以分别生成 `AIMessage.tool_calls` 和 `ToolMessage`；工具调用和结果不得仅存在于普通 text content block 中。

#### Scenario: assistant 同时返回文本和工具调用

- **WHEN** LiteLLM 返回文本、reasoning 和一个可执行工具调用
- **THEN** canonical item 层分别保留 assistant output/reasoning 与 tool call identity，LangChain projection 将文本/reasoning 放入 `AIMessage.content`、工具名称/参数/id 放入 `AIMessage.tool_calls`

#### Scenario: 工具返回大型结果

- **WHEN** 工具返回大型 JSON 结果
- **THEN** 结果作为独立 `tool_result` canonical item 保存并通过 `tool_call_id` 关联，LangChain projection 生成 `ToolMessage`，assistant output 不复制结果正文

### Requirement: 历史恢复必须执行目标 provider 投影

系统 SHALL 将 v2 canonical `assistant_output`、reasoning、tool_call 和 tool_result item 视为不可变来源，在模型请求边界根据来源 provider、目标 provider 和能力配置生成临时 LangChain/provider 投影。若一次性 `legacy_import_v1_to_v2` migration staging 读取 v1 `AIMessage`，它只能作为迁移输入；正常 provider/history/checkpoint/runtime 不读取 v1。投影至少支持独立控制可见 content、`reasoning_content`、`thinking_blocks`、reasoning item、summary 和 encrypted reasoning。

投影 SHALL 保留用户消息、可见 assistant 文本、标准工具调用和工具结果；目标 provider 不支持的 reasoning 必须过滤，不得改写 checkpoint、rollout JSONL 或源 canonical item。`assistant_text` 只属于 projection，不得作为 v2 canonical item kind。

#### Scenario: 从 reasoning provider 切换到普通模型

- **WHEN** 当前目标模型不接受 reasoning_content、thinking_blocks 和 reasoning item
- **THEN** 请求历史只携带可见 assistant text、用户输入和工具契约，原始 reasoning item/content part 仍保留在 canonical source

#### Scenario: 同一 provider 回放 encrypted reasoning

- **WHEN** 来源 provider 与目标 provider 相同，且目标能力允许 encrypted replay
- **THEN** 投影可以携带 encrypted 内容，但必须删除新的请求不能复用的 server-owned id、status、session 和 response 生命周期字段

#### Scenario: 跨 provider 只保留安全摘要

- **WHEN** 目标 provider 接受 summary 但不接受来源 encrypted payload
- **THEN** 投影只保留可安全转换的 summary，删除 encrypted 内容和来源专属字段

#### Scenario: 空 reasoning item 不参与请求历史

- **WHEN** 源 content 只有没有正文、summary 或 encrypted 内容的 reasoning item
- **THEN** 投影过滤该 item，不把 provider server id 当成可执行历史

### Requirement: 流式响应只在最终提交时形成 canonical AIMessage

流式 chunk 可以暂时携带 provider 原始片段、运行时 `part_id/index` 或额外字段，但 v2 assistant output 稳定完成时 SHALL 形成一个或多个经过 Schema 校验的 canonical item/content-part；仅在 LangChain compatibility projection 边界才合并为有序 `AIMessage.content`。最终持久化 item/message 不得因每个 chunk 重复保存 reasoning 或工具参数。

#### Scenario: 多个流式 reasoning/text/tool chunk

- **WHEN** 模型连续返回 reasoning、文本和工具参数 chunk
- **THEN** 内存合并结果按 normalized identity 形成最终 assistant output/reasoning/tool call item，LangChain projection 至多生成目标所需的 AIMessage，不为每个 chunk 追加完整正文

#### Scenario: assistant 流式过程中崩溃

- **WHEN** 进程在最终 item finalization 或 AIMessage compatibility projection 前崩溃
- **THEN** 未完成 draft 可以丢失，但此前已提交 item 保持可读，不产生多条互相矛盾的 assistant revision；未提交 output 不得被标为 final

### Requirement: checkpoint、rollout 和 Web 投影必须识别有序 content 布局

checkpoint saver、上下文恢复器、rollout item projection 和 Web 历史读取 SHALL 从经过 Schema 校验的有序 canonical content parts/items 提取可见文本、reasoning summary、reasoning 文本、encrypted 存在标记和工具阶段。只有一次性 `legacy_import_v1_to_v2` migration staging 可以读取有序 v1 `AIMessage.content` 并生成迁移报告；正常 history/provider/checkpoint/runtime 不读取 v1 carrier。不得依赖 `additional_kwargs` 的 reasoning 备份或额外 payload 文件。

Web projection SHALL 不返回 encrypted 正文；full checkpoint 恢复 SHALL 根据 active view 和 ContextRequestPlan 生成完整可执行的 LangChain message projection，模型请求投影才按 provider 能力过滤。该 plan 的 selection 必须消费 Saver 冻结的 `ContextSelectionEntry.ref` tagged union：canonical/request-only source 使用 `ContextRef`，`selection_kind=tool_set` 使用独立 `ToolSetRef`；ToolSetRef 只能进入 Provider tools/tool-config，不能生成 LangChain message 或 canonical history。Provider adapter 必须使用与 ToolSetRef 绑定的 manifest（`tool_set_snapshot_id`、source revision、content length、hash token、schema/policy version 和 policy）生成 request_hash；不能用当前 registry 或笼统 logical tool contract hash 替换它。不同 provider 的 wire tool 编码可以造成不同 request_hash，但相同 manifest 必须保留相同 plan_hash，manifest/hash mismatch 必须拒绝 exact replay。

#### Scenario: checkpoint roundtrip

- **WHEN** saver 将含 reasoning、summary、encrypted 和 tool_calls 的 v2 item set 写入后再读取
- **THEN** canonical item/content part 顺序、carrier 原始字段、工具调用和来源元数据可重建，LangChain projection 不需要额外 kwargs 备份

#### Scenario: 默认 Web 历史

- **WHEN** Web 请求 user、tool_summary、final_response 历史
- **THEN** SQLite/item projection 从 canonical reasoning/content parts 得到安全摘要和存在标记，不解析或返回 encrypted 正文

#### Scenario: 工具详情和模型 full history 分别读取

- **WHEN** 用户请求 bounded tool_call/tool_result，或 LangGraph 请求完整可执行历史
- **THEN** 前者只读取所需 item 内容，后者通过 ContextRequestPlan 的同一 selection 读取完整 source item/ToolSetSnapshot，并在目标 provider adapter 中投影；ToolSetRef 只编码到 tools/tool-config，两者都不回退到 detail store 伪造消息
