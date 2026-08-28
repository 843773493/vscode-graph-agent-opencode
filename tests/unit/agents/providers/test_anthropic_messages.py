from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessageChunk, HumanMessage

from app.agents.providers.anthropic_messages import BoxteamAnthropicMessagesModel
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)


@pytest.mark.asyncio
async def test_anthropic_semantic_delta_reaches_hook_before_langchain_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RawStream:
        def __init__(self) -> None:
            self._read = False
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._read:
                raise StopAsyncIteration
            self._read = True
            return object()

        async def close(self) -> None:
            self.closed = True

    raw_stream = RawStream()

    async def fake_acreate(_self, _payload):
        return raw_stream

    def fake_convert(_self, _event, **kwargs):
        return (
            AIMessageChunk(content=[{"type": "text", "text": "完成"}]),
            kwargs.get("block_start_event"),
        )

    monkeypatch.setattr(ChatAnthropic, "_acreate", fake_acreate)
    monkeypatch.setattr(
        ChatAnthropic,
        "_make_message_chunk_from_anthropic_event",
        fake_convert,
    )
    model = BoxteamAnthropicMessagesModel(
        model="claude-test",
        api_key="test-key",
        streaming=True,
    )
    observed: list[str] = []

    class RecordingSink:
        async def accept_message_chunk(self, chunk: AIMessageChunk) -> None:
            del chunk
            observed.append("hook")

    token = set_current_model_delta_sink(RecordingSink())
    try:
        chunks = []
        async for chunk in model._astream([HumanMessage(content="hi")]):
            observed.append("yield")
            chunks.append(chunk)
    finally:
        reset_current_model_delta_sink(token)

    assert observed == ["hook", "yield"]
    assert len(chunks) == 1
    assert raw_stream.closed is True
