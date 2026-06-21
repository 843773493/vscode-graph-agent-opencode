"""OpencodeZenChatOpenAI 单元测试。

重点验证：
- `_convert_messages_to_dicts` 把 AIMessage.content 中的结构化 reasoning 块
  展平为 `<think>...</think>` 文本，让 opencode.ai/DeepSeek 后端的 Chat
  Completions 接口可以接收多轮 reasoning 上下文。
- 不带 reasoning 块的 AIMessage 行为不变。
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.providers.opencode_zen import OpencodeZenChatOpenAI


def _make_model() -> OpencodeZenChatOpenAI:
    """构造一个不发起任何网络调用的 OpencodeZenChatOpenAI 实例。"""
    return OpencodeZenChatOpenAI(
        model="big-pickle",
        api_key="test-key",
        base_url="https://example.com/v1",
    )


def test_convert_messages_to_dicts_passthrough_when_no_reasoning_blocks():
    model = _make_model()
    messages = [
        SystemMessage(content="你是助手"),
        HumanMessage(content="hi"),
        AIMessage(content="hello"),
    ]
    dicts = model._convert_messages_to_dicts(messages)
    # role 必须是 OpenAI 标准（user/assistant/system/tool），不是 LangChain
    # 风格（human/ai），这样下游（包括 LangGraph 历史回环）直接拿到合规 dict。
    assert dicts[0] == {"role": "system", "content": "你是助手"}
    assert dicts[1] == {"role": "user", "content": "hi"}
    assert dicts[2] == {"role": "assistant", "content": "hello"}


def test_convert_messages_to_dicts_flattens_responses_api_reasoning_block():
    """来自 Responses API 历史消息的 reasoning 块应被展平为 <think>...</think>。"""
    model = _make_model()
    ai = AIMessage(
        content=[
            {
                "type": "reasoning",
                "id": "rs_abc",
                "summary": [{"type": "summary_text", "text": "思考片段1"}],
            },
            {"type": "text", "text": "最终回答"},
        ]
    )
    dicts = model._convert_messages_to_dicts([ai])
    assert len(dicts) == 1
    content = dicts[0]["content"]
    # content 仍然是 list 形式（用于下游其它组件识别），但 reasoning 块被移除
    assert isinstance(content, list)
    assert not any(b.get("type") == "reasoning" for b in content)
    # 第一个 text 块开头注入了 <think>...</think>
    text_block = next(b for b in content if b.get("type") == "text")
    assert text_block["text"].startswith("<think>\n思考片段1\n</think>\n\n")
    assert text_block["text"].endswith("最终回答")


def test_convert_messages_to_dicts_flattens_multiple_reasoning_summaries():
    """reasoning 块有多个 summary 项时，应按顺序拼接。"""
    model = _make_model()
    ai = AIMessage(
        content=[
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "片段A"},
                    {"type": "summary_text", "text": "片段B"},
                ],
            },
            {"type": "text", "text": "回答"},
        ]
    )
    dicts = model._convert_messages_to_dicts([ai])
    content = dicts[0]["content"]
    text_block = next(b for b in content if b.get("type") == "text")
    assert "<think>\n片段A\n片段B\n</think>" in text_block["text"]


def test_convert_messages_to_dicts_creates_text_block_when_only_reasoning():
    """只有 reasoning 块没有 text 块时，应自动创建一个 text 块承载 <think> 文本。"""
    model = _make_model()
    ai = AIMessage(
        content=[
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "只有思考"}],
            },
        ]
    )
    dicts = model._convert_messages_to_dicts([ai])
    content = dicts[0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "<think>\n只有思考\n</think>"


def test_flatten_reasoning_blocks_returns_original_when_not_list():
    """非 list 类型的 content 应原样返回。"""
    model = _make_model()
    assert model._flatten_reasoning_blocks("plain string") == "plain string"
    assert model._flatten_reasoning_blocks(None) is None
    assert model._flatten_reasoning_blocks(42) == 42


def test_flatten_reasoning_blocks_returns_original_when_no_reasoning_type():
    """list 类型的 content 中若无 reasoning 块，应原样返回（保持原引用）。"""
    model = _make_model()
    original = [
        {"type": "text", "text": "a"},
        {"type": "output_text", "text": "b"},
    ]
    result = model._flatten_reasoning_blocks(original)
    # 没有 reasoning 块时应原样返回（函数应避免无谓的深拷贝）
    assert result is original


def test_stream_parses_reasoning_content_from_direct_attribute():
    """流式响应中 delta.reasoning_content 可直接访问时，应正确解析 reasoning。"""
    from unittest.mock import MagicMock

    model = _make_model()

    # 构造模拟 chunk：delta 有 reasoning_content 属性
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta = MagicMock()
    chunk.choices[0].delta.content = None
    chunk.choices[0].delta.reasoning_content = "这是思考过程"
    chunk.choices[0].delta.tool_calls = None

    state = {"reasoning_started": False, "reasoning_finished": False}
    chunks = model._process_chunk(chunk, state)
    
    # 应该生成 reasoning_start 和 reasoning_delta 两个 chunk
    assert len(chunks) == 2
    assert chunks[0].message.additional_kwargs == {"kind": "reasoning", "phase": "start"}
    assert chunks[1].message.content == "这是思考过程"
    assert chunks[1].message.additional_kwargs == {"kind": "reasoning", "phase": "delta"}


def test_stream_parses_reasoning_content_from_model_extra():
    """流式响应中 delta 通过 model_extra 获取 reasoning_content 时，应正确解析 reasoning。
    
    模拟 OpenAI SDK pydantic 模型丢弃未定义字段的情况。
    """
    from unittest.mock import MagicMock

    model = _make_model()

    # 构造模拟 chunk：delta 没有 reasoning_content 属性，但 model_extra 有
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta = MagicMock()
    chunk.choices[0].delta.content = None
    # 没有 reasoning_content 属性
    delattr(chunk.choices[0].delta, "reasoning_content")
    # 通过 model_extra 提供
    chunk.choices[0].delta.model_extra = {"reasoning_content": "通过 model_extra 的思考"}
    chunk.choices[0].delta.tool_calls = None

    state = {"reasoning_started": False, "reasoning_finished": False}
    chunks = model._process_chunk(chunk, state)
    
    # 应该生成 reasoning_start 和 reasoning_delta 两个 chunk
    assert len(chunks) == 2
    assert chunks[0].message.additional_kwargs == {"kind": "reasoning", "phase": "start"}
    assert chunks[1].message.content == "通过 model_extra 的思考"
    assert chunks[1].message.additional_kwargs == {"kind": "reasoning", "phase": "delta"}
