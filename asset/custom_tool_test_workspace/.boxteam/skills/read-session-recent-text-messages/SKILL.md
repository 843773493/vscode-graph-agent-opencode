---
name: read-session-recent-text-messages
description: 当用户要求读取另一个 session_id 的 Agent State 最近 N 轮用户消息、模型 text 消息、会话历史片段，或要求调用 read_session_recent_text_messages 扩展工具时，读取本 skill。
allowed-tools: read_session_recent_text_messages
---

# read_session_recent_text_messages 扩展工具

## 适用场景

当用户给出另一个 `session_id`，并要求查看该会话最近 N 轮用户消息及期间模型文本消息时，必须使用本 skill。

## 调用方式

`read_session_recent_text_messages` 不会直接出现在模型 tools 列表中。必须发起真实工具调用，使用固定入口 `invoke_custom_tool`。

参数 schema：

```json
{
  "tool_name": "read_session_recent_text_messages",
  "arguments": {
    "session_id": "目标 session_id",
    "rounds": 5
  }
}
```

字段说明：

- `session_id`：必填，要读取的目标会话 ID。
- `rounds`：选填，最近用户轮次数，默认 `5`。

## 输出

工具返回 JSON 字符串，包含：

- `session_id`
- `rounds`
- `user_message_count`
- `messages`

`messages` 只包含用户消息和模型 `{"type":"text"}` 文本消息，不包含请求 metadata、checkpoint metadata、工具调用参数或其它内部信息。
