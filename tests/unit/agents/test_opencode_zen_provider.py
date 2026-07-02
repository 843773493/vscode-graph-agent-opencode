"""BoxteamLiteLLMChatModel 单元测试。

重点验证：
- `_convert_messages_to_dicts` 把 AIMessage.content 中的结构化 reasoning 块
  从 Chat Completions 历史消息中剥离，避免把 reasoning 混入普通 assistant 正文。
- 不带 reasoning 块的 AIMessage 行为不变。
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel


def _make_model() -> BoxteamLiteLLMChatModel:
    """构造一个不发起任何网络调用的 LiteLLM 包装实例。"""
    return BoxteamLiteLLMChatModel(
        model="openai/big-pickle",
        api_key="test-key",
        api_base="https://example.com/v1",
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


def test_convert_messages_to_dicts_strips_responses_api_reasoning_block():
    """来自 Responses API 历史消息的 reasoning 块不应混入可见正文。"""
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
    text_block = next(b for b in content if b.get("type") == "text")
    assert text_block["text"] == "最终回答"


def test_convert_messages_to_dicts_strips_multiple_reasoning_summaries():
    """reasoning 块有多个 summary 项时也不应注入正文。"""
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
    assert text_block["text"] == "回答"


def test_convert_messages_to_dicts_returns_empty_content_when_only_reasoning():
    """只有 reasoning 块没有 text 块时，发送给 Chat Completions 的正文应为空。"""
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
    assert content == ""


def test_normalize_history_content_returns_original_when_not_list():
    """非 list 类型的 content 应原样返回。"""
    model = _make_model()
    assert model._normalize_history_content("plain string") == "plain string"
    assert model._normalize_history_content(None) is None
    assert model._normalize_history_content(42) == 42


def test_normalize_history_content_returns_original_for_standard_text_blocks():
    """标准 text 块不需要转换时应原样返回（保持原引用）。"""
    model = _make_model()
    original = [
        {"type": "text", "text": "a"},
    ]
    result = model._normalize_history_content(original)
    assert result is original


def test_normalize_history_content_normalizes_output_text_without_reasoning():
    """output_text 不应透传给 Chat Completions 历史消息。"""
    model = _make_model()
    original = [
        {"type": "text", "text": "a"},
        {"type": "output_text", "text": "b"},
    ]
    result = model._normalize_history_content(original)
    assert result == [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]


def test_stream_parses_reasoning_content_from_delta():
    """流式响应中 delta.reasoning_content 应正确解析成标准 reasoning。"""
    model = _make_model()

    chunks = model._convert_stream_response_chunk(
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "这是思考过程",
                    }
                }
            ]
        },
        first_chunk_yielded=False,
    )

    assert len(chunks) == 1
    assert chunks[0].message.additional_kwargs == {}
    assert chunks[0].message.content == [
        {"type": "reasoning", "reasoning": "这是思考过程"}
    ]


def test_stream_parses_reasoning_content_from_model_extra():
    """流式响应中 delta.model_extra.reasoning_content 应正确解析 reasoning。"""
    model = _make_model()

    chunks = model._convert_stream_response_chunk(
        {
            "choices": [
                {
                    "delta": {
                        "model_extra": {
                            "reasoning_content": "通过 model_extra 的思考",
                        },
                    }
                }
            ]
        },
        first_chunk_yielded=False,
    )

    assert len(chunks) == 1
    assert chunks[0].message.additional_kwargs == {}
    assert chunks[0].message.content == [
        {"type": "reasoning", "reasoning": "通过 model_extra 的思考"}
    ]
