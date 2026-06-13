from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.schemas.event import AgentStartEvent, AgentStartPayload
from app.services.infrastructure.trace_event_store import TraceEventStore


@pytest.mark.asyncio
async def test_store_append_and_read(tmp_path: Path):
    store = TraceEventStore(logs_dir=tmp_path)
    session_id = "ses_1"

    event = AgentStartEvent(
        event_id="evt_1",
        job_id="job_1",
        session_id=session_id,
        agent_id="default",
        timestamp=datetime.now(timezone.utc),
        payload=AgentStartPayload(message="start", agent_id="default"),
    )
    store.append(session_id, event)

    events = store.read_events(session_id)
    assert len(events) == 1
    assert events[0].event_id == "evt_1"
    assert events[0].type == "agent_start"


@pytest.mark.asyncio
async def test_store_stream_new_events(tmp_path: Path):
    store = TraceEventStore(logs_dir=tmp_path)
    session_id = "ses_2"

    stream = store.stream_events(session_id)

    event = AgentStartEvent(
        event_id="evt_2",
        job_id="job_2",
        session_id=session_id,
        agent_id="default",
        timestamp=datetime.now(timezone.utc),
        payload=AgentStartPayload(message="start", agent_id="default"),
    )
    store.append(session_id, event)

    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    assert received.event_id == "evt_2"
