from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel


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
