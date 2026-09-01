from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk

from app.agents.providers.openai_responses import (
    BoxteamOpenAIResponsesModel,
    ResponsesPayloadBuildTimeoutError,
    ResponsesStreamOpenTimeoutError,
)
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)


@pytest.mark.asyncio
async def test_responses_stream_open_has_a_bounded_provider_timeout(monkeypatch):
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
    monkeypatch.setattr(
        "app.agents.providers.openai_responses.DEFAULT_RESPONSES_STREAM_OPEN_TIMEOUT_SECONDS",
        0.01,
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
    with pytest.raises(ResponsesStreamOpenTimeoutError, match="未建立事件流"):
        await asyncio.wait_for(first_chunk, timeout=0.5)


@pytest.mark.asyncio
async def test_responses_payload_preparation_does_not_block_event_loop(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    ticks = asyncio.Event()

    async def response_events():
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(usage=None),
        )

    async def fake_aresponses(**_payload):
        return response_events()

    model = BoxteamOpenAIResponsesModel(
        model="gpt-5.6-luna",
        api_base="https://chatgpt.com/backend-api/codex",
        api_key="",
        custom_llm_provider="chatgpt",
        provider_id="backup_4",
        responses_store=False,
    )
    original_payload_builder = model._responses_payload

    def slow_payload_builder(*args, **kwargs):
        entered.set()
        release.wait(timeout=1)
        return original_payload_builder(*args, **kwargs)

    monkeypatch.setattr(model, "_responses_payload", slow_payload_builder)
    monkeypatch.setattr(
        "app.agents.providers.openai_responses.litellm.aresponses",
        fake_aresponses,
    )

    async def observe_event_loop():
        await asyncio.sleep(0.01)
        ticks.set()

    stream_task = asyncio.create_task(
        anext(model._astream([HumanMessage(content="只回复 OK")]))
    )
    await asyncio.wait_for(asyncio.to_thread(entered.wait, 0.5), timeout=0.6)
    await asyncio.wait_for(observe_event_loop(), timeout=0.1)
    assert not stream_task.done()

    release.set()
    chunk = await asyncio.wait_for(stream_task, timeout=0.5)

    assert ticks.is_set()
    assert chunk.message.chunk_position == "last"


@pytest.mark.asyncio
async def test_responses_payload_timeout_is_reported_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()

    async def fake_aresponses(**_payload):
        raise AssertionError("payload 投影超时后不应调用 provider")

    model = BoxteamOpenAIResponsesModel(
        model="gpt-5.6-luna",
        api_base="https://chatgpt.com/backend-api/codex",
        api_key="",
        custom_llm_provider="chatgpt",
        provider_id="backup_4",
        responses_store=False,
    )

    def timed_out_payload(*_args, **kwargs):
        entered.set()
        assert kwargs["deadline"] > 0
        raise ResponsesPayloadBuildTimeoutError("历史 payload 投影测试超时")

    monkeypatch.setattr(model, "_responses_payload", timed_out_payload)
    monkeypatch.setattr(
        "app.agents.providers.openai_responses.litellm.aresponses",
        fake_aresponses,
    )

    stream_task = asyncio.create_task(
        anext(model._astream([HumanMessage(content="只回复 OK")]))
    )
    await asyncio.wait_for(asyncio.to_thread(entered.wait, 0.5), timeout=0.6)
    with pytest.raises(ResponsesPayloadBuildTimeoutError, match="历史 payload"):
        await asyncio.wait_for(stream_task, timeout=0.5)


@pytest.mark.asyncio
async def test_responses_payload_build_watchdog_does_not_wait_for_blocked_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    provider_called = False

    async def fake_aresponses(**_payload):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("payload 构造超时后不应调用 provider")

    model = BoxteamOpenAIResponsesModel(
        model="gpt-5.6-luna",
        api_base="https://chatgpt.com/backend-api/codex",
        api_key="",
        custom_llm_provider="chatgpt",
        provider_id="backup_4",
        responses_store=False,
    )

    def blocked_payload_builder(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=1)
        return {"model": "gpt-5.6-luna", "input": []}

    monkeypatch.setattr(model, "_responses_payload", blocked_payload_builder)
    monkeypatch.setattr(
        "app.agents.providers.openai_responses.litellm.aresponses",
        fake_aresponses,
    )
    monkeypatch.setattr(
        "app.agents.providers.openai_responses.DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS",
        0.01,
    )

    stream_task = asyncio.create_task(
        anext(model._astream([HumanMessage(content="只回复 OK")]))
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 0.5), timeout=0.6)
        with pytest.raises(ResponsesPayloadBuildTimeoutError, match="payload 构造"):
            await asyncio.wait_for(stream_task, timeout=0.5)
        assert not provider_called
    finally:
        release.set()


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
