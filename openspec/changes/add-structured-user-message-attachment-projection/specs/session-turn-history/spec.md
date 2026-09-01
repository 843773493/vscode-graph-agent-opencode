## MODIFIED Requirements

### Requirement: 历史投影区分 assistant text、reasoning 和 final response

系统 SHALL 独立支持 `user_text`、`user_attachments`、`assistant_text`、`thinking`、`tool_summary`、`tool_call`、`tool_result` 和 `final_response`。用户消息与 assistant 消息一样必须按有序 content block 提取：文本 block 进入用户可见文本，附件 block/metadata 进入独立附件 DTO，provider rich block 不得整体字符串化为正文。`thinking` 投影由 `thinking_blocks` 表达，块类型为可读 `reasoning`、provider 生成的 `summary` 或不携带正文的 `encrypted` 标记。`final_response` MUST 使用 `turn_finalize.final_message_sequence`，而不是把最后一个 assistant role 作为唯一依据；无 finalization 的旧 fixture 才允许 heuristic fallback。

#### Scenario: 混合 AIMessage

- **WHEN** 一个 AIMessage 同时包含 text content block、reasoning content block 和 tool_calls
- **THEN** canonical checkpoint 保持一条 AIMessage，历史投影可拆出 assistant text/tool summary，但 LangGraph 恢复不会得到多条伪造的 assistant 消息

#### Scenario: 混合 HumanMessage

- **WHEN** 一个 HumanMessage 同时包含文本、附件 manifest 和 `image_url` 预览 block
- **THEN** 历史投影只将文本 block 聚合为 `user_text`，从附件 metadata 返回 `user_attachments`，不把预览 block 或整个 content 数组 dump 到用户正文

#### Scenario: 思考块来源保持可区分

- **WHEN** provider 返回可展示 reasoning、provider summary 和/或 encrypted reasoning
- **THEN** Web 分别返回 `reasoning`、`summary` 和无正文的 `encrypted` 块，encrypted payload 只能用于 provider 恢复，不得出现在 API 响应

#### Scenario: 用户预览 base64 只属于完整模型历史

- **WHEN** checkpoint 的 HumanMessage 保存了模型实际使用的预览 data URL
- **THEN** full 恢复保留该 block，默认历史 projection 只返回用户文本和附件 metadata，不将 data URL 放入可见 `content`
