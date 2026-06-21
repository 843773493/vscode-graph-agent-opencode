# app/agents/providers/

## 目录用途

`app/agents/providers/` 存放 LangChain `BaseChatModel` 子类，每个子类把某家 LLM 提供商（opencode_zen、未来的 anthropic / google-genai / 其它私有后端）的"方言"流转换为 **统一格式** `ChatGenerationChunk`，供 `app/services/orchestration/agent_execution_service` 通过 SSE 推送给前端、并写入 LangGraph checkpoint 用于多轮对话。

本目录是"接入新模型"的入口：所有 provider 实现 + 它们的格式自检工具都在这里。

## 可修改内容

- **新增 provider**：新建 `xxx.py`，子类化 `ChatOpenAI` / `BaseChatModel`，实现 `_astream` 产出统一格式 chunks；并在类上加 `self_check()` 调用 `validate_provider_format(self)`，便于测试和调试。
- **新增 fixture / check 项**：在 `_format_check.py` 中追加 StreamFixture 子类或 `check_*` 纯函数；`ALL_CHECKS` 列表里登记新的检查项。
- **修正 `_format_check.py` 的判定逻辑**：当发现新场景下现有规则误判时，可调整检查函数；调整时必须同时更新 `tests/unit/agents/test_provider_format_check.py` 的正反例。
- **`__init__.py`**：可加入 provider 类的 re-export 方便外部 import。

## 不可修改内容

- **统一格式契约的语义**（见 `_format_check.py` 头部 docstring）：
  - `additional_kwargs["kind"] ∈ {None, "reasoning", "text", "tool"}`
  - `additional_kwargs["phase"] ∈ {None, "start", "delta", "end"}`（仅 reasoning 标记边界）
  - `ChatGenerationChunk.message` 必须是 `AIMessageChunk`，**不允许**是裸字符串
  - 文本 chunk 的 `content` 必须是 `str`，**不允许**是 `list[dict]`
  - reasoning 流必须 `start` → `delta` → `end` 完整成对
  - `tool_call_chunks` 必须含 `name` / `args` / `id` 至少一项
  - 任何变更必须先在 issue 中讨论，不允许单方面放松规则（会破坏 SSE 流和 checkpoint 存储）。
- **`agent_execution_service` 的事件名 / payload schema**（依赖上述契约反推）。
- **现有 fixture 的预期行为**（`ReasoningOnlyFixture` 等）：如果发现 fixture 自身有 bug，应同时修复 provider 实现和 fixture，**不**通过修改 fixture 跳过检查来掩盖问题。

## 规范

### 1. 每个 provider 必须实现 `self_check()`

参考 `opencode_zen.py` 的实现模式：构造若干 `build_stream(scenario)` fixture 模拟后端输出，跑 `validate_provider_format(self)` 拿 `FormatCheckResult`。在测试里写：

```python
def test_xxx_provider_format():
    provider = XxxChatOpenAI(...)
    result = provider.self_check()
    assert result.all_passed, result.report()
```

### 2. 失败时必须可读

`_format_check.py` 中每条 `check_*` 的 `remediation` 字段必须给出**可执行的修复提示**（具体到改哪个字段、什么值），失败时新人 30 秒内能定位。

### 3. 不要 hardcode 模型名 / API key

`api_key` / `base_url` / `model` 都走构造参数；测试里用 `example.com` 之类占位即可。

### 4. 历史消息回环必须走 `_convert_messages_to_dicts`

provider 历史消息从 LangGraph checkpoint 读出后，**必须**经过本方法的转换再发回后端；不要在调用方另写一份 role 映射（LangChain `HumanMessage.type == "human"`、AIMessage 是 `"ai"`，但 OpenAI 风格后端要 `"user"` / `"assistant"`）。`_format_check.check_history_messages_accepted` 已在自检中覆盖这条契约。

### 5. 静态检查

每次修改本目录的 .py 文件后，跑一次 `ast.parse` 验证语法（项目 venv 中未安装 ruff）：

```bash
.venv\Scripts\python.exe -c "import ast; [ast.parse(open(f, encoding='utf-8').read(), filename=f) for f in ['app/agents/providers/_format_check.py', 'app/agents/providers/xxx.py']]"
```

### 6. 测试位置

- provider 自身行为（reasoning 展平、role 映射）：`tests/unit/agents/test_<provider>.py`
- 通用格式契约 / 跨 provider 共享的检查项：`tests/unit/agents/test_provider_format_check.py`

新增 provider 时两类测试都要写。
