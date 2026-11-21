from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.execution_pause import ExecutionPause, bind_execution_pause
from app.services.orchestration.event_stream.reader import iter_agent_events


@pytest.fixture
def paused_source():
    pause = ExecutionPause()
    requested = asyncio.Event()

    class Source:
        def __init__(self, phase):
            self.phase = phase

        async def astream_events(self, *_args, **_kwargs):
            if self.phase == "model":
                yield {"event": "on_chat_model_start", "name": "ChatOpenAI"}
            pause.request()
            requested.set()
            async with pause.boundary():
                yield {"event": "on_chat_model_end", "name": "ChatOpenAI"}

    return pause, requested, Source


@pytest.mark.parametrize("phase", ["startup", "model", "dispatch"])
async def test_reader_excludes_static_pause_from_each_watchdog(paused_source, phase):
    pause, requested, source = paused_source
    runtime = SimpleNamespace(pending_tool_calls=lambda: (("call", "probe", True),)) if phase == "dispatch" else None

    async def consume():
        return [event async for event in iter_agent_events(
            agent=source(phase), input_payload={}, stream_config={},
            session_id="ses_pause", job_id="job_pause", message_stream_runtime=runtime,
            cancellation_signal=None, idle_model_timeout_seconds=0.02,
            initial_event_timeout_seconds=0.02, effective_tool_dispatch_timeout_seconds=0.02,
        )]

    with bind_execution_pause(pause):
        task = asyncio.create_task(consume())
    await requested.wait()
    await asyncio.sleep(0.06)
    assert not task.done()
    pause.resume()
    events = await task
    assert events[-1]["event"] == "on_chat_model_end"
