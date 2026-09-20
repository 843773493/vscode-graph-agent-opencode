## MODIFIED Requirements

### Requirement: 历史恢复必须执行目标 provider 投影

系统 SHALL 将持久化的 `AIMessage` 和由 `UserContentBuilder` 生成的结构化 `HumanMessage` 视为不可变来源，在模型请求边界根据来源 provider、目标 provider 和能力配置生成临时投影。投影至少支持独立控制用户文本、附件路径说明、用户 rich content、可见 assistant content、`reasoning_content`、`thinking_blocks`、reasoning item、summary 和 encrypted reasoning。

投影 SHALL 保留用户消息、附件身份和相对路径元信息、可见 assistant 文本、标准工具调用和工具结果；目标 provider 不支持的用户 rich block 或 reasoning 必须仅在请求投影中过滤，并不得改写 checkpoint、rollout JSONL 或源消息。所有 Provider 的最终模型请求 SHALL 经过 LiteLLM 的公开异步发送入口：Chat Completions 和 Anthropic Messages 使用 `litellm.acompletion`，Responses 使用 `litellm.aresponses`。provider-specific mapper 只能生成 LiteLLM 入口所需的请求输入并归一化结果，不得调用 provider SDK、原生 HTTP API 或以直连 SDK 作为回退。

对于 Chat Completions 和 Anthropic Messages，应用传给 LiteLLM 的图片 block SHALL 使用 `{"type":"image_url","image_url":{"url":"data:image/...;base64,..."}}` 形状；对于 Responses，应用传给 LiteLLM 的图片 block SHALL 使用 `{"type":"input_image","image_url":"data:image/...;base64,..."}` 形状。Anthropic 原生 `{"type":"image","source":...}` wire block 只能由 LiteLLM 在其 provider 边界内部生成，应用层不得直接构造或发送该形状。

#### Scenario: 从 reasoning provider 切换到普通模型

- **WHEN** 当前目标模型不接受 reasoning_content、thinking_blocks 和 reasoning item
- **THEN** 请求历史只携带可见文本、用户附件路径和工具契约，原始思考仍保留在源 `AIMessage.content`

#### Scenario: 同一 provider 回放 encrypted reasoning

- **WHEN** 来源 provider 与目标 provider 相同，且目标能力允许 encrypted replay
- **THEN** 投影可以携带 encrypted 内容，但必须删除新的请求不能复用的 server-owned id、status、session 和 response 生命周期字段

#### Scenario: 跨 provider 只保留安全摘要

- **WHEN** 目标 provider 接受 summary 但不接受来源 encrypted payload
- **THEN** 投影只保留可安全转换的 summary，删除 encrypted 内容和来源专属字段，同时不删除用户附件路径说明

#### Scenario: Chat Completions 图片请求统一经过 LiteLLM

- **WHEN** 目标协议为 Chat Completions，且用户消息包含有效的图片预览
- **THEN** 应用通过 `litellm.acompletion` 发送 `messages`，图片输入保持 `image_url` block，应用不直接调用目标 provider SDK，源消息保持不变

#### Scenario: Responses 图片请求使用 LiteLLM Responses 入口

- **WHEN** 目标协议为 Responses，且用户消息包含有效的图片预览
- **THEN** 应用通过 `litellm.aresponses` 发送 `input`，图片输入使用 `input_image` block 及字符串形式的 `image_url`，不得把该请求降级为 provider SDK 直连

#### Scenario: Anthropic 图片转换由 LiteLLM 完成

- **WHEN** 目标 provider 为 Anthropic Messages，且用户消息包含有效的图片预览
- **THEN** 应用通过 `litellm.acompletion` 发送 LiteLLM 可接受的 `image_url` block，不构造 Anthropic 原生 `image/source` block；最终 Anthropic wire 转换由 LiteLLM 完成

#### Scenario: LiteLLM 不支持 rich block 时禁止直连回退

- **WHEN** LiteLLM 无法为目标 provider 发送某个可选 rich block
- **THEN** 请求投影保留 manifest 和相对路径，记录 `not_sent`/`projection_failed` 诊断，并且系统不得改用 `ChatAnthropic`、其它 provider SDK 或原生 HTTP 客户端发送

#### Scenario: 图片预览 rich block 不被目标 provider 支持

- **WHEN** 源 `HumanMessage` 包含预览 `image_url` block，而目标 provider 不支持图像输入
- **THEN** 请求投影保留用户文本和相对附件路径，跳过该 rich block 并记录明确的未发送原因，源 `HumanMessage` 和 checkpoint 保持不变

#### Scenario: 空 reasoning item 不参与请求历史

- **WHEN** 源 content 只有没有正文、summary 或 encrypted 内容的 reasoning item
- **THEN** 投影过滤该 item，不把 provider server id 当成可执行历史

### Requirement: 投影不能修改 checkpoint 来源消息

历史投影 SHALL 返回新的临时消息或请求 payload，不得就地修改 `AIMessage.content`、`HumanMessage.content`、`AIMessage.tool_calls`、`AIMessage.response_metadata`、用户附件元数据、SQLite 行或 rollout JSONL。相同 checkpoint 在不同目标 provider 下重复读取，必须得到相同的源消息和确定性的目标投影。

#### Scenario: 同一历史切换两个模型

- **WHEN** 先用不支持 encrypted 或图片 rich block 的模型读取，再用支持回放的来源模型读取同一 checkpoint
- **THEN** 第一次读取不会清除第二次所需的 source content 或预览 base64，第二次仍可按能力生成回放请求
