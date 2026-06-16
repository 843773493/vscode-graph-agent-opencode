from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.schemas.event import (
    AgentStartEvent,
    AgentStartPayload,
    JobCreatedEvent,
    JobCreatedPayload,
    TextEndEvent,
    TextEndPayload,
    ToolCallEndEvent,
    ToolCallEndPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)
from app.services.infrastructure.trace_event_store import TraceEventStore


@pytest.mark.asyncio
async def test_store_append_and_read(tmp_path: Path):
    store = TraceEventStore(logs_dir=tmp_path)
    session_id = "ses_1"

    event = AgentStartEvent(
        event_id="evt_1",
        job_id="job_1",
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
        agent_id="default",
        timestamp=datetime.now(timezone.utc),
        payload=AgentStartPayload(message="start", agent_id="default"),
    )
    store.append(session_id, event)

    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    assert received.event_id == "evt_2"


@pytest.mark.asyncio
async def test_store_appends_message_trace_for_key_events(tmp_path: Path):
    store = TraceEventStore(logs_dir=tmp_path)
    session_id = "ses_3"
    now = datetime.now(timezone.utc)

    job_created = JobCreatedEvent(
        event_id="evt_job",
        job_id="job_3",
        timestamp=now,
        payload=JobCreatedPayload(session_id=session_id, message="hi", agent_id="default"),
    )
    text_end = TextEndEvent(
        event_id="evt_text",
        job_id="job_3",
        timestamp=now,
        payload=TextEndPayload(text="hello"),
    )
    tool_start = ToolCallStartEvent(
        event_id="evt_tool_start",
        job_id="job_3",
        timestamp=now,
        payload=ToolCallStartPayload(tool_name="read_file", args={"path": "foo"}, agent_id="default"),
    )
    tool_end = ToolCallEndEvent(
        event_id="evt_tool_end",
        job_id="job_3",
        timestamp=now,
        payload=ToolCallEndPayload(tool_name="read_file", result="bar", agent_id="default"),
    )
    agent_start = AgentStartEvent(
        event_id="evt_agent_start",
        job_id="job_3",
        timestamp=now,
        payload=AgentStartPayload(message="start", agent_id="default"),
    )

    for event in (job_created, text_end, tool_start, tool_end, agent_start):
        store.append(session_id, event)

    all_events = store.read_events(session_id)
    assert len(all_events) == 5

    message_events = store.read_message_events(session_id)
    assert [e.type for e in message_events] == ["job_created", "text_end", "tool_call_start", "tool_call_end"]

    message_file = tmp_path / "traces" / f"trace_message_{session_id}.jsonl"
    assert message_file.exists()


@pytest.mark.asyncio
async def test_store_stream_message_events(tmp_path: Path):
    store = TraceEventStore(logs_dir=tmp_path)
    session_id = "ses_4"

    stream = store.stream_message_events(session_id)

    event = TextEndEvent(
        event_id="evt_text",
        job_id="job_4",
        timestamp=datetime.now(timezone.utc),
        payload=TextEndPayload(text="hello"),
    )
    store.append(session_id, event)

    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    assert received.event_id == "evt_text"
    assert received.type == "text_end"
