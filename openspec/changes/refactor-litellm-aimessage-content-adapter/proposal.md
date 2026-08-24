## Why

当前 LiteLLM adapter 将同一份 provider reasoning 数据同时放入 `AIMessage.content`、`additional_kwargs` 和 `content[].extras`，导致 checkpoint JSONL 重复保存，并把 provider 原始格式与 LangChain 的标准化字段混在一起。需要将 LiteLLM 响应的持久化入口收敛到 `AIMessage.content`，在读取下一次模型上下文时再依据目标 provider 能力选择性投影，保证跨模型切换不丢失可见文本、工具调用和最终响应。

## What Changes

- 新增独立的 LiteLLM → `AIMessage` 内容适配器，使用经过 Schema 校验的有序 `AIMessage.content` block 序列保存响应中的 `content`、`reasoning_content`、`thinking_blocks` 和 `reasoning_items`。
- `reasoning_content` 和 `reasoning_items` 使用保留原始字段名的 carrier block 写入有序 content 序列，避免分离字段造成跨来源顺序丢失。
- 删除由 adapter 写入 `AIMessage.additional_kwargs` 的 `thinking_blocks`、`reasoning_items` 备份；不再把同一 provider 原始对象同时复制到多个消息字段。
- 保持 `AIMessage.tool_calls` 和 `ToolMessage.tool_call_id` 的 LangChain 语义，工具调用不能只作为任意内容块保存。
- 增加按来源 provider、目标 provider 和能力配置选择性构造模型请求历史的逻辑：可见文本、tool call、tool result 和 final response 始终保留；reasoning、summary、thinking 和 encrypted reasoning 只在目标 provider 能识别或允许回放时进入请求。
- 保留原始 provider 内容在 checkpoint 中的不可变消息内容；历史投影和模型请求投影不得修改 JSONL 或 SQLite 中的原始消息。
- 对 Responses provider 保留同 provider encrypted reasoning 的可回放字段，同时移除不可跨请求复用的服务端 ID、状态和 provider session 字段。
- 删除当前 adapter 针对 `additional_kwargs.reasoning_content`、`additional_kwargs.thinking_blocks` 和 `additional_kwargs.reasoning_items` 的持久化依赖；请求适配器统一从 `AIMessage.content` 读取内容。
- 更新 reasoning、模型切换、流式响应、checkpoint 恢复和 provider payload 测试；重新生成受影响的 rollout fixture。
- **BREAKING**：`AIMessage` 的 checkpoint 内容布局改变，旧的重复 `additional_kwargs` 形式不提供迁移兼容；原型阶段直接按新格式生成和验证测试资产。

## Capabilities

### New Capabilities

- `litellm-aimessage-content-adapter`: 定义 LiteLLM 原始响应进入 `AIMessage.content` 的单一存储入口，以及按 provider 能力选择性恢复模型请求历史的规则。

### Modified Capabilities

无。该 change 依赖 `refactor-rollout-checkpoint-storage` 提供的 rollout JSONL、SQLite projection 和 checkpoint reader，但不修改其存储控制语义。

## Impact

- Provider adapter：`app/agents/providers/litellm_chat.py`、`app/agents/providers/openai_responses.py` 及 provider payload 转换逻辑。
- Agent/LangGraph：checkpoint 恢复后的 `AIMessage`、工具调用路由、流式 `AIMessageChunk` 合并和跨模型上下文请求。
- Rollout 投影：reasoning、summary、encrypted reasoning 和工具投影需要从新的 `AIMessage.content` 布局读取。
- 测试与资产：provider 格式自检、模型切换、Responses encrypted replay、checkpoint roundtrip、历史投影和 128 Turn fixture。
