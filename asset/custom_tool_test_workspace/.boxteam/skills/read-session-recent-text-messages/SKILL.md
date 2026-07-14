---
name: read-session-recent-text-messages
description: 当用户要求读取另一个 session_id 的最近消息、grep/read 当前模型上下文 JSONL，或调用 read_session_recent_text_messages、grep_session_context_jsonl、read_session_context_jsonl 时，读取本 skill。
allowed-tools: read_session_recent_text_messages, grep_session_context_jsonl, read_session_context_jsonl
---

# Session Context 扩展工具组

## 适用场景

当用户给出另一个 `session_id`，并要求查看最近 N 轮文本或像文件一样搜索、分段读取该会话当前模型上下文时，必须使用本 skill。

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

返回的 `context_snapshot` 是轻量一致性元数据，包含 `snapshot_id`、`content_sha256`、有效和原始记录数、字节数及压缩状态。

## 搜索上下文 JSONL

```json
{
  "tool_name": "grep_session_context_jsonl",
  "arguments": {
    "session_id": "目标 session_id",
    "pattern": "正则表达式",
    "case_sensitive": false,
    "max_matches": 20,
    "expected_snapshot_id": "上一步返回的 snapshot_id"
  }
}
```

grep 返回匹配行号、匹配列、短预览和行哈希，不返回整条超长记录。

## 分段读取上下文 JSONL

```json
{
  "tool_name": "read_session_context_jsonl",
  "arguments": {
    "session_id": "目标 session_id",
    "line_start": 1,
    "line_count": 20,
    "max_chars_per_line": 4000,
    "expected_snapshot_id": "grep 返回的 snapshot_id"
  }
}
```

read 返回带行号的 JSONL 记录，以及 `has_more` 和 `next_line_start`。

## 一致性规则

- 第一次调用记录 `context_snapshot.snapshot_id`。
- 后续 grep/read 把它作为 `expected_snapshot_id` 传入。
- `consistency=changed` 表示期间新增消息或发生压缩；必须放弃旧行号和旧结果，从新快照重新 grep/read。
- `consistency=matched` 才能安全组合多次读取结果。

## 输出

工具返回 JSON 字符串，包含：

- `session_id`
- `rounds`
- `user_message_count`
- `messages`
- `context_snapshot`

`messages` 只包含用户消息和模型 `{"type":"text"}` 文本消息，不包含请求 metadata、checkpoint metadata、工具调用参数或其它内部信息。
