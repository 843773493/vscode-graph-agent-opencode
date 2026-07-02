from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from app.agents.providers._format_check import (
    ALL_CHECKS,
    check_chunks,
    check_chunks_are_aimessage_chunks,
    check_content_blocks_are_standard,
    check_history_messages_accepted,
    check_no_private_stream_markers,
    check_stream_merges_without_private_marker_noise,
    check_tool_call_chunks_have_required_fields,
)
from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel


def _chunk(
    content: object = "",
    *,
    additional_kwargs: dict[str, object] | None = None,
    tool_call_chunks: list[dict] | None = None,
) -> ChatGenerationChunk:
    return ChatGenerationChunk(
        message=AIMessageChunk(
            content=content,
            additional_kwargs=additional_kwargs or {},
            tool_call_chunks=tool_call_chunks or [],
        )
    )


def _make_test_provider() -> BoxteamLiteLLMChatModel:
    return BoxteamLiteLLMChatModel(
        model="openai/big-pickle",
        api_key="test-key",
        api_base="https://example.com/v1",
    )


def test_chunks_must_be_aimessage_chunks_passes():
    assert check_chunks_are_aimessage_chunks([_chunk("hi")]).passed


def test_chunks_must_be_aimessage_chunks_fails_on_raw_object():
    class FakeChunk:
        pass

    item = check_chunks_are_aimessage_chunks([FakeChunk()])  # type: ignore[list-item]

    assert not item.passed
    assert "0" in item.detail


def test_private_stream_markers_are_rejected():
    item = check_no_private_stream_markers(
        [_chunk("hi", additional_kwargs={"kind": "reasoning", "phase": "delta"})]
    )

    assert not item.passed
    assert "kind/phase" in item.name


def test_standard_reasoning_and_text_blocks_pass():
    chunks = [
        _chunk([{"type": "reasoning", "reasoning": "思考"}]),
        _chunk([{"type": "text", "text": "回答"}]),
    ]

    assert check_content_blocks_are_standard(chunks).passed


def test_invalid_content_block_fails():
    item = check_content_blocks_are_standard(
        [_chunk([{"type": "thinking", "text": "思考"}])]
    )

    assert not item.passed
    assert "thinking" in item.detail


def test_chunk_merge_has_no_private_marker_noise():
    chunks = [
        _chunk([{"type": "reasoning", "reasoning": "A"}]),
        _chunk([{"type": "reasoning", "reasoning": "B"}]),
        _chunk([{"type": "text", "text": "C"}]),
    ]

    assert check_stream_merges_without_private_marker_noise(chunks).passed


def test_chunk_merge_rejects_private_marker_noise():
    chunks = [
        _chunk("A", additional_kwargs={"kind": "reasoning"}),
        _chunk("B", additional_kwargs={"kind": "reasoning"}),
    ]

    item = check_stream_merges_without_private_marker_noise(chunks)

    assert not item.passed
    assert "additional_kwargs" in item.detail


def test_tool_call_chunks_with_valid_fields_passes():
    chunks = [
        _chunk(
            "",
            tool_call_chunks=[{"name": "ls", "args": "{}", "id": "call_1"}],
        )
    ]

    assert check_tool_call_chunks_have_required_fields(chunks).passed


def test_tool_call_chunks_without_any_field_fails():
    item = check_tool_call_chunks_have_required_fields(
        [_chunk("", tool_call_chunks=[{"index": 0}])]
    )

    assert not item.passed
    assert "name" in item.remediation


def test_check_chunks_runs_all_checks():
    chunks = [
        _chunk([{"type": "reasoning", "reasoning": "x"}]),
        _chunk([{"type": "text", "text": "y"}]),
    ]
    items = check_chunks(chunks)

    assert len(items) == len(ALL_CHECKS)
    for item in items:
        assert item.passed, f"{item.name}: {item.detail}"


def test_history_roundtrip_with_reasoning_blocks_passes():
    provider = _make_test_provider()
    messages = [
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "历史推理"}],
                },
                {"type": "output_text", "text": "历史回答"},
            ],
            response_metadata={"model_provider": "openai"},
        )
    ]

    item = check_history_messages_accepted(provider, messages)

    assert item.passed, item.detail


def test_history_roundtrip_missing_converter_fails_with_clear_message():
    class NoConverter:
        pass

    item = check_history_messages_accepted(NoConverter(), [AIMessage(content="x")])

    assert not item.passed
    assert "_convert_messages_to_dicts" in item.name or "_convert_messages_to_dicts" in item.remediation


def test_history_roundtrip_rejects_unconverted_content_blocks():
    class BadConverter:
        def _convert_messages_to_dicts(self, messages):
            return [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "reasoning": "不能直传"},
                    ],
                }
            ]

    item = check_history_messages_accepted(BadConverter(), [AIMessage(content="x")])

    assert not item.passed
    assert "reasoning" in item.detail


def test_litellm_self_check_passes():
    provider = _make_test_provider()
    result = provider.self_check()

    assert result.all_passed, result.report()


@pytest.mark.asyncio
async def test_litellm_fixture_stream_uses_standard_blocks():
    provider = _make_test_provider()
    chunks = []
    async for chunk in provider.build_stream("mixed_reasoning_text"):
        chunks.append(chunk)

    assert chunks[0].message.additional_kwargs == {}
    assert chunks[0].message.content == [{"type": "reasoning", "reasoning": "思考"}]
    assert chunks[-1].message.content == [{"type": "text", "text": "回答"}]
