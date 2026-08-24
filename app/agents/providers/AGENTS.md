# app/agents/providers/

## 目录用途

`app/agents/providers/` 存放 LangChain `BaseChatModel` 包装层。Chat Completions 走 LiteLLM `completion/acompletion`，OpenAI Responses API 走 LiteLLM `responses/aresponses`；本目录负责把各模型输出统一成经过 `message_content_schema.py` 校验的 **LangChain 有序 content blocks**，供 `app/services/orchestration/agent_execution_service` 通过 SSE 推送给前端、并写入 LangGraph checkpoint 用于多轮对话。

本目录是"接入新模型"的入口：所有 provider 实现 + 它们的格式自检工具都在这里。

## 可修改内容

- **新增 provider 行为**：优先扩展 `litellm_chat.py` 中的配置映射或 content block 规范化逻辑；只有 LiteLLM 无法覆盖时，才新增 `BaseChatModel` 包装类。
- **新增 fixture / check 项**：在 `_format_check.py` 中追加 StreamFixture 子类或 `check_*` 纯函数；`ALL_CHECKS` 列表里登记新的检查项。
- **修正 `_format_check.py` 的判定逻辑**：当发现新场景下现有规则误判时，可调整检查函数；调整时必须同时更新 `tests/unit/agents/test_provider_format_check.py` 的正反例。
- **`__init__.py`**：可加入 provider 类的 re-export 方便外部 import。

## 不可修改内容

- **统一格式契约的语义**（见 `_format_check.py`）：
  - `ChatGenerationChunk.message` 必须是 `AIMessageChunk`，**不允许**是裸字符串。
  - 最终可持久化的 `AIMessage.content` 是有序 block 数组。LiteLLM 的 `reasoning_content` 和 `reasoning_items` 分别放在同名 carrier block 中；`text`、`thinking`、`redacted_thinking` 和 provider 支持的媒体块直接整体复制。Responses reasoning item 保留在 `reasoning_items` carrier 内（包括未知 provider 扩展），不再套 `litellm_payload` 或 `extras.response_item`。
  - 流式合并阶段可以临时使用 `part_*`、`index` 和 `extras` 保存增量身份；消息进入 checkpoint 前必须由 canonicalizer 删除这些运行时字段，并恢复有序 carrier/provider blocks。
  - 不允许把流式分类写入 `additional_kwargs["kind"]` / `additional_kwargs["phase"]`；工具调用必须放入 `AIMessage.tool_calls`。
  - `tool_call_chunks` 必须含 `name` / `args` / `id` 至少一项。
  - 任何变更必须同步更新 `_format_check.py` 和 provider 测试，不能通过放宽检查掩盖格式错误。
- **`agent_execution_service` 的事件名 / payload schema**（依赖上述契约反推）。
- **现有 fixture 的预期行为**（`ReasoningOnlyFixture` 等）：如果发现 fixture 自身有 bug，应同时修复 provider 实现和 fixture，**不**通过修改 fixture 跳过检查来掩盖问题。

## 规范

### 1. 每个 provider 必须实现 `self_check()`

参考 `litellm_chat.py` 的实现模式：构造若干 `build_stream(scenario)` fixture 模拟后端输出，跑 `validate_provider_format(self)` 拿 `FormatCheckResult`。在测试里写：

```python
def test_xxx_provider_format():
    provider = BoxteamLiteLLMChatModel(...)
    result = provider.self_check()
    assert result.all_passed, result.report()
```

### 2. 失败时必须可读

`_format_check.py` 中每条 `check_*` 的 `remediation` 字段必须给出**可执行的修复提示**（具体到改哪个字段、什么值），失败时新人 30 秒内能定位。

### 3. 不要 hardcode 模型名 / API key

`api_key` / `api_base` / `model` 都走构造参数；测试里用 `example.com` 之类占位即可。

### 4. 历史消息回环必须走 `_convert_messages_to_dicts`

provider 历史消息从 LangGraph checkpoint 读出后，**必须**经过本方法的转换再发回后端；不要在调用方另写一份 role 映射（LangChain `HumanMessage.type == "human"`、AIMessage 是 `"ai"`，但 OpenAI 风格后端要 `"user"` / `"assistant"`）。`_format_check.check_history_messages_accepted` 已在自检中覆盖这条契约。

### 5. 测试位置

- provider 自身行为（reasoning 剥离、历史 content 归一化、role 映射）：`tests/unit/agents/test_<provider>.py`
- 通用格式契约 / 跨 provider 共享的检查项：`tests/unit/agents/test_provider_format_check.py`

新增 provider 时两类测试都要写。
