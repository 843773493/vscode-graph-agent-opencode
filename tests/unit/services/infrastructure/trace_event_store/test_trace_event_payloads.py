from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.identifier import create_prefixed_id
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


@pytest.mark.asyncio
async def test_store_appends_message_trace_for_key_events(
    tmp_path: Path,
    trace_event_store_factory,
):
    session_id = "ses_019b7ac404a57fbc8553730565da5b9e"
    store, session_dir = trace_event_store_factory(session_id)
    now = datetime.now(UTC)

    job_created = JobCreatedEvent(
        event_id="evt_job",
        job_id="job_3",
        timestamp=now,
        payload=JobCreatedPayload(session_id=session_id, thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id), message="hi", agent_id="default"),
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
async def test_store_stream_message_events(tmp_path: Path, trace_event_store_factory):
    session_id = "ses_019c1c9fcd4479dd826a58516b537118"
    store, _ = trace_event_store_factory(session_id)

    stream = store.stream_message_events(session_id)

    event = TextEndEvent(
        event_id="evt_text",
        part_id="part_text",
        job_id="job_4",
        timestamp=datetime.now(UTC),
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
    trace_event_store_factory,
):
    store, _ = trace_event_store_factory("ses_019be193ee33734eb2580cb970f434dc")
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
        timestamp=datetime.now(UTC),
        payload=AgentStartPayload(message="start", agent_id="default"),
    )
    timer = threading.Timer(0.2, release_write.set)
    timer.start()
    started_at = time.monotonic()
    append_task = asyncio.create_task(store.append("ses_019be193ee33734eb2580cb970f434dc", event))

    await asyncio.sleep(0.02)
    assert time.monotonic() - started_at < 0.1

    await asyncio.wait_for(append_task, timeout=1.0)
    timer.cancel()


def test_read_events_rejects_legacy_events_without_part_identity(
    tmp_path: Path,
    trace_event_store_factory,
):
    session_id = "ses_019c4ae91d6a71ec8407f65a47e2c841"
    store, session_dir = trace_event_store_factory(session_id)
    trace_file = session_dir / "logs" / "traces" / "events.jsonl"
    trace_file.parent.mkdir(parents=True)
    base = {
        "job_id": "job_legacy",
        "step_id": None,
        "agent_id": "default",
        "timestamp": datetime.now(UTC).isoformat(),
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
async def test_store_rejects_manual_session_move_before_writing(
    tmp_path: Path,
    trace_event_store_factory,
):
    session_id = "ses_019c1b1ac4ab71c58cbf27325d839746"
    store, source = trace_event_store_factory(session_id)
    resolver = get_session_path_resolver(tmp_path)

    resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '手工移动目标').node_id)
    # SQLite catalog 是唯一权威；把日期桶目录手工挪到 sessions 树外，
    # 解析必须 fail closed，不扫描物理树。
    target = tmp_path / "手工挪走" / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)

    with pytest.raises(RuntimeError, match="会话物理目录缺失"):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id="evt_after_move",
                job_id="job_after_move",
                agent_id="default",
                timestamp=datetime.now(UTC),
                payload=AgentStartPayload(message="moved", agent_id="default"),
            ),
        )

    assert not (target / "logs" / "traces" / "events.jsonl").exists()
    assert not source.exists()
