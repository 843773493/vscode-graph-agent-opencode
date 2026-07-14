from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import json

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
from app.services.infrastructure.trace_event_store import (
    TraceCursorGoneError,
    TraceEventStore,
)


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
    await store.append(session_id, event)

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
    await store.append(session_id, event)

    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    assert received.event_id == "evt_2"


@pytest.mark.asyncio
async def test_store_reads_and_streams_after_event_cursor(tmp_path: Path):
    store = TraceEventStore(logs_dir=tmp_path)
    session_id = "ses_cursor"
    now = datetime.now(timezone.utc)

    for index in range(3):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id=f"evt_{index}",
                job_id="job_cursor",
                agent_id="default",
                timestamp=now,
                payload=AgentStartPayload(message=f"start {index}", agent_id="default"),
            ),
        )

    assert [event.event_id for event in store.read_events(session_id, "evt_0")] == [
        "evt_1",
        "evt_2",
    ]

    stream = store.stream_events(session_id, "evt_1")
    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    assert received.event_id == "evt_2"
    await stream.aclose()


def test_store_rejects_missing_event_cursor(tmp_path: Path):
    store = TraceEventStore(logs_dir=tmp_path)
    session_id = "ses_missing_cursor"

    with pytest.raises(TraceCursorGoneError, match="evt_missing"):
        store.ensure_cursor(session_id, "evt_missing")

    with pytest.raises(TraceCursorGoneError, match="evt_missing"):
        store.read_events(session_id, "evt_missing")


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
        part_id="part_text",
        job_id="job_3",
        timestamp=now,
        payload=TextEndPayload(kind="markdown", text="hello"),
    )
    tool_start = ToolCallStartEvent(
        event_id="evt_tool_start",
        part_id="part_tool",
        job_id="job_3",
        timestamp=now,
        payload=ToolCallStartPayload(tool_name="read_file", args={"path": "foo"}, agent_id="default"),
    )
    tool_end = ToolCallEndEvent(
        event_id="evt_tool_end",
        part_id="part_tool",
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
        await store.append(session_id, event)

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
        part_id="part_text",
        job_id="job_4",
        timestamp=datetime.now(timezone.utc),
        payload=TextEndPayload(kind="markdown", text="hello"),
    )
    await store.append(session_id, event)

    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    assert received.event_id == "evt_text"
    assert received.type == "text_end"


@pytest.mark.asyncio
async def test_store_file_write_does_not_block_event_loop(monkeypatch, tmp_path: Path):
    store = TraceEventStore(logs_dir=tmp_path)
    release_write = threading.Event()
    original_append = store._append_event_files

    def slow_append(session_id, event):
        release_write.wait(timeout=1.0)
        original_append(session_id, event)

    monkeypatch.setattr(store, "_append_event_files", slow_append)
    event = AgentStartEvent(
        event_id="evt_slow_disk",
        job_id="job_slow_disk",
        agent_id="default",
        timestamp=datetime.now(timezone.utc),
        payload=AgentStartPayload(message="start", agent_id="default"),
    )
    timer = threading.Timer(0.2, release_write.set)
    timer.start()
    started_at = time.monotonic()
    append_task = asyncio.create_task(store.append("ses_slow_disk", event))

    await asyncio.sleep(0.02)
    assert time.monotonic() - started_at < 0.1

    await asyncio.wait_for(append_task, timeout=1.0)
    timer.cancel()


def test_read_events_migrates_legacy_response_parts(tmp_path: Path):
    store = TraceEventStore(logs_dir=tmp_path)
    session_id = "ses_legacy_parts"
    trace_file = tmp_path / "traces" / f"trace_{session_id}.jsonl"
    trace_file.parent.mkdir(parents=True)
    base = {
        "job_id": "job_legacy",
        "step_id": None,
        "agent_id": "default",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    legacy_events = [
        {**base, "event_id": "evt_1", "type": "text_start", "payload": {}},
        {
            **base,
            "event_id": "evt_2",
            "type": "text_delta",
            "payload": {"kind": "reasoning", "text": "先分析"},
        },
        {
            **base,
            "event_id": "evt_3",
            "type": "text_delta",
            "payload": {"kind": "text", "text": "先"},
        },
        {
            **base,
            "event_id": "evt_3_end",
            "type": "text_end",
            "payload": {"text": "先"},
        },
        {**base, "event_id": "evt_3b", "type": "text_start", "payload": {}},
        {
            **base,
            "event_id": "evt_3c",
            "type": "text_delta",
            "payload": {"kind": "text", "text": "说明\n\n"},
        },
        {
            **base,
            "event_id": "evt_4",
            "type": "tool_call_start",
            "payload": {
                "tool_name": "read_file",
                "tool_call_run_id": "run_legacy",
                "args": {"file_path": "README.md"},
            },
        },
        {
            **base,
            "event_id": "evt_5",
            "type": "tool_call_end",
            "payload": {
                "tool_name": "read_file",
                "tool_call_run_id": "run_legacy",
                "result": "ok",
            },
        },
        {
            **base,
            "event_id": "evt_6",
            "type": "text_end",
            "payload": {"text": "最终回复"},
        },
    ]
    trace_file.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in legacy_events),
        encoding="utf-8",
    )

    events = store.read_events(session_id)

    part_events = [event for event in events if event.type.startswith("text_")]
    assert part_events
    assert all(event.part_id for event in part_events)
    assert all(
        event.payload.kind in {"markdown", "reasoning"}
        for event in part_events
    )
    markdown_deltas = [
        event
        for event in part_events
        if event.type == "text_delta" and event.payload.kind == "markdown"
    ]
    assert len({event.part_id for event in markdown_deltas}) == 1
    tool_events = [event for event in events if event.type.startswith("tool_call_")]
    assert [event.part_id for event in tool_events] == ["run_legacy", "run_legacy"]
    assert all(
        "tool_call_run_id" not in event.payload.model_dump()
        for event in tool_events
    )
    assert events[-1].event_id == "evt_6"
