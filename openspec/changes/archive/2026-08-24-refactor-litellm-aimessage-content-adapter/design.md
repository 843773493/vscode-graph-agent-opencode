## Context

LiteLLM 的 Chat Completions、Responses 和 Anthropic 适配器需要把模型输出交给 LangChain，同时保留 reasoning summary、thinking block、Responses encrypted item 和工具调用。旧实现把这些字段放进项目自定义 `litellm_payload`，并在流式中额外复制到 `extras` 或 `additional_kwargs`，导致 checkpoint 保存格式与 provider 请求格式混在一起。

本 change 只定义 provider message 边界。rollout JSONL、SQLite、checkpoint、context view 和 fork 的权威模型仍由 `refactor-rollout-checkpoint-storage` 负责；它们读取下面定义的、经过 Schema 校验的有序 `AIMessage.content`。

## Goals

- `AIMessage.content` 是唯一的 assistant provider 内容来源，并且是保留原始顺序的 block 序列。
- LiteLLM 的 `reasoning_content` 与 `reasoning_items` 使用同名 carrier block 放入该序列；provider 已经返回的 thinking、redacted thinking、text 等 block 原样深复制。
- `AIMessage.tool_calls`、`ToolMessage.tool_call_id` 保持 LangGraph 标准契约。
- provider request projection 是读取时的临时结果，不修改源消息。
- 流式中可有临时运行字段，但 finalizer 清理后只提交一条稳定 AIMessage。
- SQLite/Web 只从同一份有序 content 建立 reasoning、文本和安全 encrypted 投影。

## Non-Goals

- 不增加旧 `litellm_payload`、旧 `extras.response_item` 或 `additional_kwargs` reasoning 的兼容读取。
- 不把 encrypted reasoning 解密或展示给 Web。
- 不改变 rollout SQLite 表、checkpoint channel、fork 或 context view 设计。
- 不把工具调用放入普通内容块。

## Decisions

### 1. Ordered content block canonical format

适配器最终生成的 content 使用经过运行时 Pydantic Schema 校验的有序列表。LiteLLM 的两个“独立响应字段”必须在列表中各自占据一个位置，否则无法表达它们与正文、thinking block 的相对顺序：

```json
{
  "content": [
    {
      "type": "reasoning_content",
      "reasoning_content": "已确定检查范围。"
    },
    {
      "type": "reasoning_items",
      "reasoning_items": [
        {
          "type": "reasoning",
          "id": "rs_item_002",
          "status": "completed",
          "content": [{"type": "reasoning_text", "text": "检查范围已确定。"}],
          "summary": [{"type": "summary_text", "text": "检查范围已确定。"}],
          "encrypted_content": "..."
        }
      ]
    },
    {"type": "thinking", "thinking": "检查工具返回值。"},
    {"type": "text", "text": "最终回答"}
  ],
  "tool_calls": [],
  "invalid_tool_calls": []
}
```

真实的 LangChain `AIMessage` 中，`content` 和 `tool_calls` 仍是两个字段；上例只是展示两者的并列边界。

Chat Completions 的纯字符串 `reasoning_content` 转为一个 `type=reasoning_content` carrier block，Responses 的 `reasoning_items` 转为一个 `type=reasoning_items` carrier block。carrier 只负责在 canonical content 中保留 LiteLLM 字段名与顺序，不把它们伪造为统一的 `type=reasoning`。LiteLLM 返回的 provider-native `thinking`、`redacted_thinking`、`text` 和媒体 block 直接深复制，不从中抽取已知字段后重建；因此 provider 新增字段会自然保留。缺失、`null` 和空值不生成空 block；只有实际存在的 provider 内容才进入 canonical content。

适配器不接受也不生成 `type=litellm_payload` 的上游 envelope。若 LangChain 在响应对象的临时字段中携带 provider reasoning，LiteLLM 响应入口必须先把它转换为上述 carrier/provider block；canonicalizer 不把 `additional_kwargs` 当作第二份消息来源。

来源 provider 不写入 content。适配器通过 LangChain 的 `response_metadata.provider_id` 保存来源，投影时要求该字段可验证；`source_model` 同样属于 response metadata 或请求审计数据。

### 2. Tool calls remain separate

LiteLLM 的可执行 tool call 由适配器规范化到 `AIMessage.tool_calls`，至少保留 name、args、id 和 index。工具结果为 `ToolMessage`，通过 `tool_call_id` 关联。content 可以有模型在调用工具前后的文本/reasoning，但不得再次复制工具参数。`invalid_tool_calls` 按 LangChain `AIMessage` 标准字段保留：非空数组记录无法解析的工具调用及原始参数；空数组虽然没有业务内容，也必须进入示例、rollout 和 checkpoint 消息，使 canonical 消息可以无损 roundtrip。

### 3. Streaming temporary state and finalizer

流式 adapter 允许使用以下仅存于内存的字段：

- `AIMessageChunk.additional_kwargs` 中的 LiteLLM delta 暂存字段；
- 为 SSE 合并和界面显示生成的 `part_id`、`index`；
- `extras.provider_part_id` 用于把运行时 part 重新关联到 provider item。

`canonicalize_ai_message()` 在稳定提交前执行：

1. 合并 reasoning/text/tool delta；
2. 从 `extras.response_item` 或 `extras.thinking_block` 恢复 provider 原始 block（只处理本次内存态），整体复制 provider block，不按字段白名单重建；
3. 删除生成的 `part_*`、运行时 index、provider_part_id、所有 `extras` 和 reasoning additional kwargs；
4. 将 LiteLLM 暂存的 `reasoning_content`、`reasoning_items` 放回同名 carrier block；保留 reasoning item 的完整字段，包括 id/status/content/summary/encrypted 以及未知 provider 扩展字段；
5. 生成一条 Schema 校验过的有序 content block 列表和一次 `AIMessage.tool_calls`。

Responses 的 `response.output_item.added` 若没有正文、summary 或 encrypted 内容，只更新流状态，不生成空消息；后续 delta/done 才形成 block。

### 4. Provider projection

公共模块 `app/agents/providers/message_content_schema.py` 提供唯一的运行时 Schema 和 `validate_content_blocks()`；公共模块 `app/agents/providers/litellm_content.py` 提供两类函数：

- `build_ai_message_content()`：把一次 LiteLLM 响应构造成 Schema 校验过的有序 canonical blocks；
- `project_ai_message_content()`：从不可变源 content 生成目标 provider 的临时 `content`、`reasoning_content`、`thinking_blocks` 和 `reasoning_items`，这些返回字段只存在于请求投影，不重新写入源消息。

投影规则：

| 源 block | Chat Completions | Responses | Anthropic |
| --- | --- | --- | --- |
| text/refusal | 可见 content | message content | message content |
| reasoning_content/reasoning_items carrier | reasoning_content（声明能力时） | reasoning item（声明能力时） | 不直接发送 |
| thinking | thinking_blocks（声明能力时） | 按目标协议过滤 | thinking block（声明能力时） |
| redacted_thinking/encrypted | 仅同 provider 且显式允许 | 仅同 provider 且显式允许 | 仅同 provider 且显式允许 |

投影必须深复制，不得修改源 AIMessage。server-owned id、status、session 和 response 生命周期字段只在请求投影时移除；rollout canonical content 仍保留原始 item。

可见文本块也必须经过同一套请求投影清理，不能把流式 `extras`、`part_*` 或 provider server id 带入目标协议；这类字段只服务于本次 SSE 合并。

Responses 历史如果同时有 reasoning item 和最终文本，适配器将 reasoning item 作为独立 input item，再生成 assistant 文本消息，避免把 reasoning 当成 assistant 正文。没有正文的 reasoning item 不加入请求历史。

### 5. SQLite/Web projection

`reasoning_projection_rows()` 只生成有限投影：

- 普通 reasoning 文本：`kind=reasoning`；
- Responses summary：`kind=summary`；
- redacted/encrypted：`kind=encrypted`，只保存长度和 hash；
- encrypted 正文不进入 Web DTO。

`visible_text()` 只读取 text/output_text/refusal，carrier 本身不显示。checkpoint full restore 保留完整 AIMessage；历史 projection/detail/full 由 rollout reader 读取同一源内容，不能新增 payload 文件或回退到旧 trace 解析。

### 6. Failure and source immutability

目标 provider 不接受某个 block 时，按能力配置过滤并保留可见文本、工具调用和工具结果；不通过删除源 block 来“修复”历史。流式请求在最终提交前崩溃可以丢失未完成内容，已提交消息必须保持可读。

## Risks / Trade-offs

- 直接 content 不是所有 provider 都能直接接受，因此请求边界必须显式调用 projection。
- Responses 原始 item 可能带 server-owned 字段，保存与回放必须区分；保存保留原始值，回放过滤不可复用状态。
- 未知 provider block 不应猜测语义，默认过滤并保留源消息，以避免跨模型误发送。
- 工具调用的标准字段独立于 content，牺牲了“所有输出只有一个 JSON 字段”的表面统一，但保证 LangGraph 可执行。

## Test Strategy

- provider unit tests：reasoning carrier/text、thinking/redacted、Responses item、summary/encrypted、空 added event、tool call 和跨 provider projection；断言不存在 `litellm_payload`。
- streaming tests：多 chunk 合并后只保留有序 carrier/provider blocks，运行时 `part_id/index/extras` 不进入最终消息。
- checkpoint roundtrip：AIMessage/ToolMessage、SQLite reasoning/tool projection、final response 指针和源消息不可变性。
- integration fixture：使用 `asset/custom_tool_test_workspace` 的复制品，验证混合 provider 128 Turn rollout 使用有序 content blocks，不依赖 payload 文件。
- 每次修改 provider 源码运行 provider pytest、ruff/AST 检查；无真实模型 E2E 要求。

## Migration Plan

原型阶段不提供旧格式迁移。重新生成测试 rollout 和 SQLite；旧 `litellm_payload` 数据不作为新 adapter 的输入。
