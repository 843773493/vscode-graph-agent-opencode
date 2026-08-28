from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk

from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)


@pytest.mark.asyncio
async def test_backup_4_first_chunk_waits_at_litellm_aresponses(monkeypatch):
    upstream_entered = asyncio.Event()
    upstream_released = asyncio.Event()

    async def response_events():
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(usage=None),
        )

    async def fake_aresponses(**payload):
        assert payload["model"] == "gpt-5.6-luna"
        assert payload["custom_llm_provider"] == "chatgpt"
        upstream_entered.set()
        await upstream_released.wait()
        return response_events()

    monkeypatch.setattr(
        "app.agents.providers.openai_responses.litellm.aresponses",
        fake_aresponses,
    )
    model = BoxteamOpenAIResponsesModel(
        model="gpt-5.6-luna",
        api_base="https://chatgpt.com/backend-api/codex",
        api_key="",
        custom_llm_provider="chatgpt",
        provider_id="backup_4",
        litellm_session_id="ses_latency_probe",
        responses_store=False,
        responses_include=["reasoning.encrypted_content"],
    )

    first_chunk = asyncio.create_task(
        anext(model._astream([HumanMessage(content="只回复 OK")]))
    )
    await asyncio.wait_for(upstream_entered.wait(), timeout=0.5)
    await asyncio.sleep(0)
    assert not first_chunk.done()

    upstream_released.set()
    chunk = await asyncio.wait_for(first_chunk, timeout=0.5)

    assert chunk.message.chunk_position == "last"


@pytest.mark.asyncio
async def test_responses_semantic_delta_reaches_hook_before_langchain_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def response_events():
        yield SimpleNamespace(type="response.output_text.delta")
        yield SimpleNamespace(type="response.completed", response=SimpleNamespace(usage=None))

    async def fake_aresponses(**_payload):
        return response_events()

    def fake_convert(
        _self,
        event,
        *,
        current_index,
        current_output_index,
        current_sub_index,
        part_state,
        original_schema,
    ):
        del part_state, original_schema
        if event.type == "response.completed":
            return current_index, current_output_index, current_sub_index, None
        return (
            current_index,
            current_output_index,
            current_sub_index,
            ChatGenerationChunk(
                message=AIMessageChunk(
                    content=[{"type": "text", "text": "完成"}],
                )
            ),
        )

    monkeypatch.setattr(
        "app.agents.providers.openai_responses.litellm.aresponses",
        fake_aresponses,
    )
    monkeypatch.setattr(
        BoxteamOpenAIResponsesModel,
        "_convert_response_event",
        fake_convert,
    )
    model = BoxteamOpenAIResponsesModel(
        model="gpt-test",
        api_key="test-key",
        custom_llm_provider="openai",
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
