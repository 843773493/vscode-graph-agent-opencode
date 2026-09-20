## MODIFIED Requirements

### Requirement: 最新 Turn 先于旧历史完整呈现

客户端 SHALL 在会话切换时先呈现最新 Turn summary；用户消息的可见文本和附件卡片元数据可以随 summary 一起显示，完整中间消息、原始 provider block 和完整附件正文只在用户明确请求或点击附件资源后按需请求，历史页和完整 Trace 不得阻塞这一过程。新用户消息的展示文本 SHALL 从结构化 block projection 得到，不读取或复制 `display_content`；内部消息仍按既有 metadata policy 处理。

#### Scenario: 切换到长会话

- **WHEN** bootstrap 返回最新 Turn summary
- **THEN** 最新用户输入、附件预览占位/缩略图、最终响应和活动统计先显示，完整中间消息保持未加载，直到用户明确展开该 Turn

#### Scenario: 用户消息包含模型预览 base64

- **WHEN** 最新用户消息的 checkpoint content 包含 `image_url` data URL
- **THEN** 客户端只使用附件 metadata 请求缩略图并渲染附件卡片，Composer/用户消息正文不渲染原始 data URL 或 JSON block
