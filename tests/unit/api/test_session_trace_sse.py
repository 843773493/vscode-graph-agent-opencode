from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from app.api.sessions import _stream_trace_sse
from app.schemas.public_v2.trace import TraceEventDTO


async def _idle_trace_events() -> AsyncIterator[tuple[TraceEventDTO, str]]:
    await asyncio.Event().wait()
    if False:
        yield _trace_event(), "tc1.idle"


async def _single_trace_event() -> AsyncIterator[tuple[TraceEventDTO, str]]:
    yield _trace_event(), "tc1.heartbeat"


def _trace_event() -> TraceEventDTO:
    return TraceEventDTO(
        event_id="evt_sse_heartbeat",
        session_id="ses_sse_heartbeat",
        job_id="job_sse_heartbeat",
        type="job_started",
        phase="job",
        title="任务开始",
        content="",
        timestamp=datetime(2026, 7, 24, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_trace_sse_sends_heartbeat_while_idle() -> None:
    stream = _stream_trace_sse(
        _idle_trace_events(),
        heartbeat_interval_seconds=0.01,
    )

    chunk = await asyncio.wait_for(anext(stream), timeout=0.2)

    assert chunk == ": heartbeat\n\n"
    await stream.aclose()


@pytest.mark.asyncio
async def test_trace_sse_emits_complete_event_block() -> None:
    stream = _stream_trace_sse(
        _single_trace_event(),
        heartbeat_interval_seconds=1,
    )

    chunk = await asyncio.wait_for(anext(stream), timeout=0.2)

    assert chunk.startswith("id: tc1.heartbeat\nevent: trace\ndata: ")
    assert '"session_id":"ses_sse_heartbeat"' in chunk
    assert chunk.endswith("\n\n")
    await stream.aclose()


@pytest.mark.asyncio
async def test_trace_sse_rejects_non_positive_heartbeat_interval() -> None:
    stream = _stream_trace_sse(
        _single_trace_event(),
        heartbeat_interval_seconds=0,
    )

    with pytest.raises(ValueError, match="心跳间隔"):
        await anext(stream)
