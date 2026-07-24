from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import json

import pytest

from app.core.path_utils import get_session_path_resolver
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


def _create_store(tmp_path: Path, session_bundle_factory, session_id: str):
    session_dir = session_bundle_factory(tmp_path, session_id)
    return TraceEventStore(sessions_dir=tmp_path), session_dir


@pytest.mark.asyncio
async def test_store_append_and_read(tmp_path: Path, session_bundle_factory):
    session_id = "ses_1"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)

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
async def test_store_stream_new_events(tmp_path: Path, session_bundle_factory):
    session_id = "ses_2"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)

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
async def test_store_reads_and_streams_after_event_cursor(
    tmp_path: Path,
    session_bundle_factory,
):
    session_id = "ses_cursor"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
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


@pytest.mark.asyncio
async def test_store_reads_only_latest_trace_tail(tmp_path: Path, session_bundle_factory):
    store, _ = _create_store(tmp_path, session_bundle_factory, "ses_tail")
    now = datetime.now(timezone.utc)
    for index in range(12):
        await store.append(
            "ses_tail",
            AgentStartEvent(
                event_id=f"evt_tail_{index}",
                job_id="job_tail",
                agent_id="default",
                timestamp=now,
                payload=AgentStartPayload(
                    message=f"start {index}",
                    agent_id="default",
                ),
            ),
        )

    events = store.read_events("ses_tail", tail_limit=4)

    assert [event.event_id for event in events] == [
        "evt_tail_8",
        "evt_tail_9",
        "evt_tail_10",
        "evt_tail_11",
    ]


def test_store_rejects_missing_event_cursor(tmp_path: Path, session_bundle_factory):
    session_id = "ses_missing_cursor"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)

    with pytest.raises(TraceCursorGoneError, match="evt_missing"):
        store.ensure_cursor(session_id, "evt_missing")

    with pytest.raises(TraceCursorGoneError, match="evt_missing"):
        store.read_events(session_id, "evt_missing")


@pytest.mark.asyncio
async def test_store_appends_message_trace_for_key_events(
    tmp_path: Path,
    session_bundle_factory,
):
    session_id = "ses_3"
    store, session_dir = _create_store(tmp_path, session_bundle_factory, session_id)
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
        payload=ToolCallStartPayload(
            execution_id="run_tool",
            tool_name="read_file",
            args={"path": "foo"},
            agent_id="default",
        ),
    )
    tool_end = ToolCallEndEvent(
        event_id="evt_tool_end",
        part_id="part_tool",
        job_id="job_3",
        timestamp=now,
        payload=ToolCallEndPayload(
            execution_id="run_tool",
            tool_call_id="call_tool",
            tool_name="read_file",
            result="bar",
            agent_id="default",
        ),
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

    message_file = session_dir / "logs" / "traces" / "messages.jsonl"
    assert message_file.exists()


@pytest.mark.asyncio
async def test_store_stream_message_events(tmp_path: Path, session_bundle_factory):
    session_id = "ses_4"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)

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
async def test_store_file_write_does_not_block_event_loop(
    monkeypatch,
    tmp_path: Path,
    session_bundle_factory,
):
    store, _ = _create_store(tmp_path, session_bundle_factory, "ses_slow_disk")
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


def test_read_events_rejects_legacy_events_without_part_identity(
    tmp_path: Path,
    session_bundle_factory,
):
    session_id = "ses_legacy_parts"
    store, session_dir = _create_store(tmp_path, session_bundle_factory, session_id)
    trace_file = session_dir / "logs" / "traces" / "events.jsonl"
    trace_file.parent.mkdir(parents=True)
    base = {
        "job_id": "job_legacy",
        "step_id": None,
        "agent_id": "default",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    legacy_events = [
        {
            **base,
            "event_id": "evt_legacy",
            "type": "text_delta",
            "payload": {"kind": "reasoning", "text": "先分析"},
        }
    ]
    trace_file.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in legacy_events),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Trace 事件协议无效"):
        store.read_events(session_id)


@pytest.mark.asyncio
async def test_store_follows_manual_session_move_before_writing(
    tmp_path: Path,
    session_bundle_factory,
):
    session_id = "ses_manual_trace_move"
    store, source = _create_store(tmp_path, session_bundle_factory, session_id)
    resolver = get_session_path_resolver(tmp_path)
    folder = resolver.create_folder(name="手工移动目标", parent_node_id=None)
    target = folder.path / source.name
    source.replace(target)

    await store.append(
        session_id,
        AgentStartEvent(
            event_id="evt_after_move",
            job_id="job_after_move",
            agent_id="default",
            timestamp=datetime.now(timezone.utc),
            payload=AgentStartPayload(message="moved", agent_id="default"),
        ),
    )

    assert (target / "logs" / "traces" / "events.jsonl").is_file()
    assert not source.exists()
