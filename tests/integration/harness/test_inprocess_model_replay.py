"""回放 owner 关闭再重建时，真实 SDK 不得重用已经关闭的 HTTP client。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from app.testing.model_stream.context import ModelCallIdentity, replay_model_call
from tests.integration.conftest import inprocess_model_replay


@pytest.fixture
def replay_owner_factory(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """在同一 loop 顺序运行正式 fixture 的两个完整生命周期。"""
    run_id = os.getenv("BOXTEAM_TEST_RUN_ID", "replay-owner-lifetime")
    ordinal = 0

    @asynccontextmanager
    async def owner():
        nonlocal ordinal
        ordinal += 1
        with monkeypatch.context() as owner_patch:
            owner_patch.setenv("BOXTEAM_TEST_RUN_ID", f"{run_id}-owner-{ordinal}")
            fixture = inprocess_model_replay.__wrapped__(request, owner_patch)
            try:
                yield await anext(fixture)
            finally:
                await fixture.aclose()

    return owner


@pytest.mark.asyncio
async def test_sequential_replay_owners_use_live_sdk_clients(replay_owner_factory):
    import litellm

    session = "ses_00000000400070008000000000000021"
    thread = "thr_00000000400070008000000000000022"
    endpoint = "https://replay-cache.invalid/v1"
    original_cache = litellm.in_memory_llm_clients_cache
    clients = []
    for ordinal in (1, 2):
        async with replay_owner_factory() as (replay, controller):
            cassette = build_cassette(
                asset_id=f"owner-{ordinal}", url=endpoint + "/chat/completions",
                model="gpt-4.1", frames=chat_completion_frames(
                    response_id=f"owner-{ordinal}", model="gpt-4.1",
                    deltas=({"role": "assistant", "content": f"owner-{ordinal}"},),
                    finish_reason="stop",
                ),
            )
            (call_id,) = replay.control.plan_model_calls(
                session, thread, [cassette.raw["interactions"][0]],
            )
            controller.ensure_installed()
            with replay_model_call(ModelCallIdentity(session, thread, call_id)):
                stream = await litellm.acompletion(
                    model="openai/gpt-4.1", api_base=endpoint, api_key="replay-cache-key",
                    messages=[{"role": "user", "content": "验证真实 SDK client 生命周期"}],
                    stream=True, num_retries=0,
                )
                content = "".join([chunk.choices[0].delta.content or "" async for chunk in stream])
            assert content == f"owner-{ordinal}"
            assert controller.transport.call_count == 1
            assert list(controller.transport.hit_counts.values()) == [1]
            clients.append(controller.client)
        assert clients[-1] is not None and clients[-1].is_closed
        assert litellm.in_memory_llm_clients_cache is original_cache
    assert clients[0] is not clients[1]
