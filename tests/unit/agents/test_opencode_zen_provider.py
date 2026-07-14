"""BoxteamLiteLLMChatModel 单元测试。

重点验证：
- `_convert_messages_to_dicts` 把 AIMessage.content 中的结构化 reasoning 块
  从 Chat Completions 历史消息中剥离，避免把 reasoning 混入普通 assistant 正文。
- 不带 reasoning 块的 AIMessage 行为不变。
"""
from __future__ import annotations

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_chunk_to_message,
)

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel


@pytest.fixture
def model() -> BoxteamLiteLLMChatModel:
    """构造一个不发起任何网络调用的 LiteLLM 包装实例。"""
    return BoxteamLiteLLMChatModel(
        model="openai/big-pickle",
        api_key="test-key",
        api_base="https://example.com/v1",
    )


def test_convert_messages_to_dicts_passthrough_when_no_reasoning_blocks(
    model: BoxteamLiteLLMChatModel,
):
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


def test_convert_messages_to_dicts_strips_responses_api_reasoning_block(
    model: BoxteamLiteLLMChatModel,
):
    """来自 Responses API 历史消息的 reasoning 块不应混入可见正文。"""
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


def test_convert_messages_to_dicts_strips_multiple_reasoning_summaries(
    model: BoxteamLiteLLMChatModel,
):
    """reasoning 块有多个 summary 项时也不应注入正文。"""
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


def test_convert_messages_to_dicts_returns_empty_content_when_only_reasoning(
    model: BoxteamLiteLLMChatModel,
):
    """只有 reasoning 块没有 text 块时，发送给 Chat Completions 的正文应为空。"""
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


def test_normalize_history_content_returns_original_when_not_list(
    model: BoxteamLiteLLMChatModel,
):
    """非 list 类型的 content 应原样返回。"""
    assert model._normalize_history_content("plain string") == "plain string"
    assert model._normalize_history_content(None) is None
    assert model._normalize_history_content(42) == 42


def test_normalize_history_content_returns_original_for_standard_text_blocks(
    model: BoxteamLiteLLMChatModel,
):
    """标准 text 块不需要转换时应原样返回（保持原引用）。"""
    original = [
        {"type": "text", "text": "a"},
    ]
    result = model._normalize_history_content(original)
    assert result is original


def test_normalize_history_content_normalizes_output_text_without_reasoning(
    model: BoxteamLiteLLMChatModel,
):
    """output_text 不应透传给 Chat Completions 历史消息。"""
    original = [
        {"type": "text", "text": "a"},
        {"type": "output_text", "text": "b"},
    ]
    result = model._normalize_history_content(original)
    assert result == [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]


def test_stream_parses_reasoning_content_from_delta(
    model: BoxteamLiteLLMChatModel,
):
    """流式响应中 delta.reasoning_content 应正确解析成标准 reasoning。"""

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


def test_stream_parses_reasoning_content_from_model_extra(
    model: BoxteamLiteLLMChatModel,
):
    """流式响应中 delta.model_extra.reasoning_content 应正确解析 reasoning。"""

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


def test_split_tool_arguments_merge_into_one_structured_call(
    model: BoxteamLiteLLMChatModel,
):
    first = model._delta_to_message_chunks(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-split",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"file_path":',
                    },
                }
            ]
        }
    )[0]
    second = model._delta_to_message_chunks(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "function": {"arguments": '"src/main.py"}'},
                }
            ]
        }
    )[0]

    message = message_chunk_to_message(first + second)

    assert isinstance(message, AIMessage)
    assert message.tool_calls == [
        {
            "name": "read_file",
            "args": {"file_path": "src/main.py"},
            "id": "call-split",
            "type": "tool_call",
        }
    ]


def test_streamed_plain_text_chunks_concatenate_without_injected_newlines(
    model: BoxteamLiteLLMChatModel,
):
    first = model._delta_to_message_chunks({"content": "AG"})[0]
    second = model._delta_to_message_chunks({"content": "ENT_END"})[0]

    message = message_chunk_to_message(first + second)

    assert message.content == "AGENT_END"


def test_stream_usage_maps_litellm_cached_tokens_to_langchain_metadata(
    model: BoxteamLiteLLMChatModel,
):
    chunks = model._convert_stream_response_chunk(
        {
            "choices": [{"delta": {}}],
            "usage": {
                "prompt_tokens": 3477,
                "completion_tokens": 84,
                "total_tokens": 3561,
                "prompt_tokens_details": {"cached_tokens": 3456},
            },
        },
        first_chunk_yielded=True,
    )

    assert len(chunks) == 1
    assert chunks[0].message.usage_metadata == {
        "input_tokens": 3477,
        "output_tokens": 84,
        "total_tokens": 3561,
        "input_token_details": {"cache_read": 3456},
    }


def test_stream_usage_accepts_provider_cache_hit_fallback_field(
    model: BoxteamLiteLLMChatModel,
):
    chunks = model._convert_stream_response_chunk(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "total_tokens": 105,
                "prompt_cache_hit_tokens": 80,
            },
        },
        first_chunk_yielded=True,
    )

    assert chunks[0].message.usage_metadata == {
        "input_tokens": 100,
        "output_tokens": 5,
        "total_tokens": 105,
        "input_token_details": {"cache_read": 80},
    }


def test_split_malformed_arguments_become_invalid_structured_call(
    model: BoxteamLiteLLMChatModel,
):
    first = model._delta_to_message_chunks(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-broken-split",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"file_path":',
                    },
                }
            ]
        }
    )[0]
    second = model._delta_to_message_chunks(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "function": {"arguments": "broken}"},
                }
            ]
        }
    )[0]

    message = message_chunk_to_message(first + second)

    assert isinstance(message, AIMessage)
    assert message.tool_calls == []
    assert message.invalid_tool_calls[0]["id"] == "call-broken-split"
    assert message.invalid_tool_calls[0]["name"] == "read_file"


def test_malformed_tool_call_and_error_result_remain_paired_in_model_history(
    model: BoxteamLiteLLMChatModel,
):
    raw_tool_call = {
        "id": "call-invalid-history",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"file_path": broken}',
        },
    }
    assistant = AIMessage(
        content="",
        additional_kwargs={"tool_calls": [raw_tool_call]},
    )
    error = ToolMessage(
        content="参数不是合法 JSON，请修正后重试。",
        name="read_file",
        tool_call_id="call-invalid-history",
        status="error",
    )

    history = model._convert_messages_to_dicts([assistant, error])

    assert history == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [raw_tool_call],
        },
        {
            "role": "tool",
            "content": "参数不是合法 JSON，请修正后重试。",
            "tool_call_id": "call-invalid-history",
            "name": "read_file",
        },
    ]
