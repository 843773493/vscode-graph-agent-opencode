from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.schemas.event import (
    AgentStartEvent,
    AgentStartPayload,
    JobCompletedEvent,
    JobCompletedPayload,
    JobCreatedEvent,
    JobCreatedPayload,
    MessageCreatedEvent,
    MessageCreatedPayload,
    SessionInterruptedEvent,
    SessionInterruptedPayload,
    StatusChangeEvent,
    StatusChangePayload,
    TextDeltaEvent,
    TextDeltaPayload,
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
from app.services.infrastructure.turn_history.trace_index import TraceTurnIndex
from app.services.infrastructure.turn_history.trace_page import (
    TracePageBudgetExceededError,
)
from app.services.infrastructure.turn_history.trace_writer import TraceEventWriter


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
        timestamp=datetime.now(UTC),
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
        timestamp=datetime.now(UTC),
        payload=AgentStartPayload(message="start", agent_id="default"),
    )
    await store.append(session_id, event)

    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    assert received.event.event_id == "evt_2"


@pytest.mark.asyncio
async def test_store_reads_and_streams_after_event_cursor(
    tmp_path: Path,
    session_bundle_factory,
):
    session_id = "ses_cursor"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)

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

    cursor_stream = store.stream_events(session_id)
    await asyncio.wait_for(cursor_stream.asend(None), timeout=2.0)
    second_record = await asyncio.wait_for(cursor_stream.asend(None), timeout=2.0)
    await cursor_stream.aclose()
    stream = store.stream_events(session_id, second_record.cursor)
    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    assert received.event.event_id == "evt_2"
    await stream.aclose()


@pytest.mark.asyncio
async def test_store_reads_only_latest_trace_tail(tmp_path: Path, session_bundle_factory):
    store, _ = _create_store(tmp_path, session_bundle_factory, "ses_tail")
    now = datetime.now(UTC)
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


@pytest.mark.asyncio
async def test_trace_diagnostic_page_reads_tail_then_older_with_opaque_cursor(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_trace_page"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    for index in range(8):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id=f"evt_page_{index}",
                job_id="job_trace_page",
                timestamp=now,
                payload=AgentStartPayload(message=f"page {index}", agent_id="default"),
            ),
        )

    latest = store.read_trace_page(session_id, cursor=None, limit=3)
    assert [event.event_id for event in latest.events] == [
        "evt_page_5",
        "evt_page_6",
        "evt_page_7",
    ]
    assert latest.has_more is True
    assert latest.next_cursor is not None
    assert latest.next_cursor.startswith("tp1.")
    assert "evt_page_5" not in latest.next_cursor

    older = store.read_trace_page(
        session_id,
        cursor=latest.next_cursor,
        limit=3,
    )
    assert [event.event_id for event in older.events] == [
        "evt_page_2",
        "evt_page_3",
        "evt_page_4",
    ]
    assert older.has_more is True
    assert older.next_cursor != latest.next_cursor


@pytest.mark.asyncio
async def test_trace_diagnostic_page_obeys_fixed_read_budget_without_read_text(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_trace_page_budget"
    store, session_dir = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    for index in range(20):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id=f"evt_budget_{index}",
                job_id="job_trace_budget",
                timestamp=now,
                payload=AgentStartPayload(message="x" * 64, agent_id="default"),
            ),
        )
    trace_file = session_dir / "logs" / "traces" / "events.jsonl"
    original_read_text = Path.read_text

    def reject_trace_read_text(path: Path, *args, **kwargs):
        if path == trace_file:
            raise AssertionError("Trace 诊断分页不得全量 read_text")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_trace_read_text)
    page = store.read_trace_page(
        session_id,
        cursor=None,
        limit=20,
        max_bytes=2048,
    )

    assert page.events
    assert page.bytes_read <= 2048
    assert page.has_more is True


@pytest.mark.asyncio
async def test_trace_diagnostic_page_stale_cursor_fails_fast(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_trace_page_stale"
    store, session_dir = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    for index in range(2):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id=f"evt_stale_{index}",
                job_id="job_trace_stale",
                timestamp=now,
                payload=AgentStartPayload(message="stale", agent_id="default"),
            ),
        )
    page = store.read_trace_page(session_id, cursor=None, limit=1)
    assert page.next_cursor is not None
    trace_file = session_dir / "logs" / "traces" / "events.jsonl"
    trace_file.write_bytes(trace_file.read_bytes().replace(b"evt_stale_1", b"evt_stale_x"))

    with pytest.raises(TraceCursorGoneError):
        store.read_trace_page(session_id, cursor=page.next_cursor, limit=1)


@pytest.mark.asyncio
async def test_trace_diagnostic_page_rejects_event_larger_than_byte_budget(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_trace_page_oversized"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    await store.append(
        session_id,
        AgentStartEvent(
            event_id="evt_oversized",
            job_id="job_trace_oversized",
            timestamp=datetime.now(UTC),
            payload=AgentStartPayload(message="x" * 4096, agent_id="default"),
        ),
    )

    with pytest.raises(TracePageBudgetExceededError):
        store.read_trace_page(
            session_id,
            cursor=None,
            limit=1,
            max_bytes=1024,
        )


@pytest.mark.asyncio
async def test_turn_migration_snapshot_uses_immutable_message_boundary(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_migration_snapshot"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    first = JobCreatedEvent(
        event_id="evt_snapshot_1",
        job_id="job_snapshot_1",
        timestamp=datetime.now(UTC),
        payload=JobCreatedPayload(
            session_id=session_id,
            message="first",
            agent_id="default",
        ),
    )
    second = JobCreatedEvent(
        event_id="evt_snapshot_2",
        job_id="job_snapshot_2",
        timestamp=datetime.now(UTC),
        payload=JobCreatedPayload(
            session_id=session_id,
            message="second",
            agent_id="default",
        ),
    )
    await store.append(session_id, first)
    snapshot = store.capture_turn_migration_snapshot(session_id)
    await store.append(session_id, second)

    migrated = list(
        store.iter_message_events(
            session_id,
            before_offset=snapshot.message_trace_size,
        )
    )

    assert snapshot.event_cursor == first.event_id
    assert [event.event_id for event in migrated] == [first.event_id]
    assert [event.event_id for event in store.iter_message_events(session_id)] == [
        first.event_id,
        second.event_id,
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
    now = datetime.now(UTC)

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
        timestamp=datetime.now(UTC),
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
    session_bundle_factory,
):
    session_id = "ses_manual_trace_move"
    store, source = _create_store(tmp_path, session_bundle_factory, session_id)
    resolver = get_session_path_resolver(tmp_path)
    folder = resolver.create_folder(name="手工移动目标", parent_node_id=None)
    target = folder.path / source.name
    source.replace(target)

    with pytest.raises(RuntimeError, match="绕过软件修改会话目录结构"):
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


@pytest.mark.asyncio
async def test_turn_bootstrap_index_keeps_job_start_beyond_128_events(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_long_turn_index"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    created = JobCreatedEvent(
        event_id="evt_long_created",
        job_id="job_long",
        timestamp=now,
        payload=JobCreatedPayload(
            session_id=session_id,
            message="long",
            agent_id="default",
        ),
    )
    await store.append(session_id, created)
    for index in range(140):
        await store.append(
            session_id,
            StatusChangeEvent(
                event_id=f"evt_long_status_{index:03d}",
                job_id="job_long",
                timestamp=now,
                payload=StatusChangePayload(
                    status="running",
                    reason=f"step-{index}",
                    session_id=session_id,
                ),
            ),
        )

    batch = store.read_turn_bootstrap_batch(
        session_id,
        max_events=128,
        max_bytes=256 * 1024,
    )

    assert batch.events[0].event.event_id == created.event_id
    assert batch.events[-1].event.event_id == "evt_long_status_139"
    assert len(batch.events) <= 128
    assert batch.has_older_events is True


@pytest.mark.asyncio
async def test_turn_bootstrap_uses_compact_index_without_parsing_huge_trace_line(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_huge_indexed_line"
    store, session_dir = _create_store(tmp_path, session_bundle_factory, session_id)
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_huge_indexed",
            job_id="job_huge_indexed",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id,
                message="x" * (2 * 1024 * 1024),
                agent_id="default",
            ),
        ),
    )

    def reject_full_event_read(*args, **kwargs):
        raise AssertionError("bootstrap 不得读取完整 Trace 大行")

    monkeypatch.setattr(TraceTurnIndex, "_read_source_event", reject_full_event_read)
    batch = store.read_turn_bootstrap_batch(
        session_id,
        max_events=128,
        max_bytes=64 * 1024,
    )

    assert len(batch.events) == 1
    assert len(batch.events[0].event.payload.message) <= 2048
    assert batch.has_older_events is True
    assert (session_dir / "logs" / "traces" / "events.jsonl").stat().st_size > 2 * 1024 * 1024


def test_legacy_huge_line_returns_partial_shell_without_reading_payload(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_huge_legacy_line"
    store, session_dir = _create_store(tmp_path, session_bundle_factory, session_id)
    event = JobCreatedEvent(
        event_id="evt_huge_legacy",
        job_id="job_huge_legacy",
        timestamp=datetime.now(UTC),
        payload=JobCreatedPayload(
            session_id=session_id,
            message="x" * (2 * 1024 * 1024),
            agent_id="default",
        ),
    )
    traces = session_dir / "logs" / "traces"
    traces.mkdir(parents=True)
    line = event.model_dump_json().encode("utf-8") + b"\n"
    (traces / "events.jsonl").write_bytes(line)
    message_path = traces / "messages.jsonl"
    message_path.write_bytes(line)
    original_read_bytes = Path.read_bytes

    def bounded_read(path: Path) -> bytes:
        if path == message_path:
            raise AssertionError("超预算 legacy 大行不得 read_bytes")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", bounded_read)
    batch = store.read_turn_bootstrap_batch(
        session_id,
        max_events=128,
        max_bytes=64 * 1024,
    )

    assert batch.events == []
    assert batch.has_older_events is True
    assert batch.index_available is False


@pytest.mark.asyncio
async def test_turn_index_recovers_commit_after_trace_files_were_flushed(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_index_commit_recovery"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    original_commit = TraceTurnIndex.commit

    def fail_commit(self, prepared):
        raise OSError("模拟 index manifest 提交前崩溃")

    monkeypatch.setattr(TraceTurnIndex, "commit", fail_commit)
    with pytest.raises(OSError, match="提交前崩溃"):
        await store.append(
            session_id,
            JobCreatedEvent(
                event_id="evt_index_recover",
                job_id="job_index_recover",
                timestamp=datetime.now(UTC),
                payload=JobCreatedPayload(
                    session_id=session_id,
                    message="recover",
                    agent_id="default",
                ),
            ),
        )
    monkeypatch.setattr(TraceTurnIndex, "commit", original_commit)

    batch = store.read_turn_bootstrap_batch(
        session_id,
        max_events=128,
        max_bytes=64 * 1024,
    )
    assert [item.event.event_id for item in batch.events] == ["evt_index_recover"]

    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_after_index_recover",
            job_id="job_after_index_recover",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id,
                message="after recover",
                agent_id="default",
            ),
        ),
    )
    recovered = store.read_turn_bootstrap_batch(
        session_id,
        max_events=128,
        max_bytes=64 * 1024,
    )
    assert recovered.events[-1].event.event_id == "evt_after_index_recover"


@pytest.mark.asyncio
@pytest.mark.parametrize("cutpoint", ["after_index", "after_message", "partial_trace"])
async def test_next_append_recovers_incomplete_trace_transaction(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
    cutpoint: str,
) -> None:
    session_id = f"ses_trace_cutpoint_{cutpoint}"
    store, session_dir = _create_store(
        tmp_path,
        session_bundle_factory,
        session_id,
    )
    original_append = TraceEventWriter._append_bytes

    def crash_at_cutpoint(file: Path, payload: bytes, *, durable: bool) -> int:
        if cutpoint == "after_index" and file.name == "messages.jsonl":
            raise OSError("模拟 index 落盘后崩溃")
        if file.name == "events.jsonl" and cutpoint == "after_message":
            raise OSError("模拟 message 落盘后崩溃")
        if file.name == "events.jsonl" and cutpoint == "partial_trace":
            with file.open("ab") as stream:
                stream.write(payload[: max(1, len(payload) // 2)])
                stream.flush()
            raise OSError("模拟 trace 尾行部分写入后崩溃")
        return original_append(file, payload, durable=durable)

    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(crash_at_cutpoint),
    )
    with pytest.raises(OSError, match="模拟"):
        await store.append(
            session_id,
            JobCreatedEvent(
                event_id="evt_cutpoint_failed",
                job_id="job_cutpoint_failed",
                timestamp=datetime.now(UTC),
                payload=JobCreatedPayload(
                    session_id=session_id,
                    message="failed",
                    agent_id="default",
                ),
            ),
        )
    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(original_append),
    )

    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_cutpoint_next",
            job_id="job_cutpoint_next",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id,
                message="next",
                agent_id="default",
            ),
        ),
    )

    assert [event.event_id for event in store.read_events(session_id)] == [
        "evt_cutpoint_next"
    ]
    assert [event.event_id for event in store.read_message_events(session_id)] == [
        "evt_cutpoint_next"
    ]
    assert not (session_dir / "logs" / "traces" / "events.jsonl").read_bytes().startswith(
        b'{"event_id":"evt_cutpoint_failed"'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cutpoint", ["after_index", "after_message", "partial_trace"])
async def test_non_indexed_append_recovers_incomplete_trace_transaction(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
    cutpoint: str,
) -> None:
    session_id = f"ses_non_indexed_recovery_{cutpoint}"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        TextDeltaEvent(
            event_id="evt_preserved_delta",
            part_id="part_preserved_delta",
            job_id="job_preserved_delta",
            timestamp=now,
            payload=TextDeltaPayload(kind="markdown", text="keep"),
        ),
    )
    original_append = TraceEventWriter._append_bytes

    def crash_at_cutpoint(file: Path, payload: bytes, *, durable: bool) -> int:
        if cutpoint == "after_index" and file.name == "messages.jsonl":
            raise OSError("模拟 index 落盘后崩溃")
        if file.name == "events.jsonl" and cutpoint == "after_message":
            raise OSError("模拟 message 落盘后崩溃")
        if file.name == "events.jsonl" and cutpoint == "partial_trace":
            with file.open("ab") as stream:
                stream.write(payload[: max(1, len(payload) // 2)])
                stream.flush()
            raise OSError("模拟 trace 尾行部分写入后崩溃")
        return original_append(file, payload, durable=durable)

    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(crash_at_cutpoint),
    )
    with pytest.raises(OSError, match="模拟"):
        await store.append(
            session_id,
            JobCreatedEvent(
                event_id="evt_failed_semantic",
                job_id="job_failed_semantic",
                timestamp=now,
                payload=JobCreatedPayload(
                    session_id=session_id,
                    message="failed",
                    agent_id="default",
                ),
            ),
        )
    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(original_append),
    )

    await store.append(
        session_id,
        AgentStartEvent(
            event_id="evt_non_indexed_next",
            job_id="job_non_indexed_next",
            timestamp=now,
            payload=AgentStartPayload(message="next", agent_id="default"),
        ),
    )
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_indexed_after_non_indexed",
            job_id="job_indexed_after_non_indexed",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                message="indexed after recovery",
                agent_id="default",
            ),
        ),
    )

    assert [event.event_id for event in store.read_events(session_id)] == [
        "evt_preserved_delta",
        "evt_non_indexed_next",
        "evt_indexed_after_non_indexed",
    ]
    assert [event.event_id for event in store.read_message_events(session_id)] == [
        "evt_indexed_after_non_indexed"
    ]


@pytest.mark.asyncio
async def test_partial_non_indexed_tail_is_repaired_before_restart_append_and_stream(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_partial_non_indexed"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        TextDeltaEvent(
            event_id="evt_complete_delta",
            part_id="part_complete_delta",
            job_id="job_partial_non_indexed",
            timestamp=now,
            payload=TextDeltaPayload(kind="markdown", text="keep"),
        ),
    )
    original_append = TraceEventWriter._append_bytes

    def partial_non_indexed(file: Path, payload: bytes, *, durable: bool) -> int:
        if file.name == "events.jsonl":
            with file.open("ab") as stream:
                stream.write(payload[: max(1, len(payload) // 2)])
                stream.flush()
            raise OSError("模拟非索引事件半行崩溃")
        return original_append(file, payload, durable=durable)

    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(partial_non_indexed),
    )
    with pytest.raises(OSError, match="非索引事件半行"):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id="evt_partial_agent_start",
                job_id="job_partial_non_indexed",
                timestamp=now,
                payload=AgentStartPayload(message="partial", agent_id="default"),
            ),
        )
    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(original_append),
    )

    restarted_store = TraceEventStore(sessions_dir=tmp_path)
    await restarted_store.append(
        session_id,
        AgentStartEvent(
            event_id="evt_after_restart",
            job_id="job_partial_non_indexed",
            timestamp=now,
            payload=AgentStartPayload(message="after", agent_id="default"),
        ),
    )
    stream = restarted_store.stream_events(session_id)
    first = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    second = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    await stream.aclose()

    assert [first.event.event_id, second.event.event_id] == [
        "evt_complete_delta",
        "evt_after_restart",
    ]


@pytest.mark.asyncio
async def test_partial_trace_tail_invading_committed_watermark_fails_fast(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_committed_trace_intrusion"
    store, session_dir = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_committed",
            job_id="job_committed",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                message="committed",
                agent_id="default",
            ),
        ),
    )
    trace_file = session_dir / "logs" / "traces" / "events.jsonl"
    payload = trace_file.read_bytes()
    assert payload.endswith(b"\n")
    trace_file.write_bytes(payload[:-1] + b" ")

    with pytest.raises(RuntimeError, match="侵入已提交语义水位"):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id="evt_rejected_after_intrusion",
                job_id="job_committed",
                timestamp=now,
                payload=AgentStartPayload(message="reject", agent_id="default"),
            ),
        )


@pytest.mark.asyncio
async def test_next_append_succeeds_when_caller_crashes_after_index_manifest(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_after_manifest_cutpoint"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    original_commit = TraceTurnIndex.commit
    failed = False

    def commit_then_crash(self, prepared):
        nonlocal failed
        original_commit(self, prepared)
        if not failed:
            failed = True
            raise OSError("模拟 manifest 落盘后调用方崩溃")

    monkeypatch.setattr(TraceTurnIndex, "commit", commit_then_crash)
    with pytest.raises(OSError, match="manifest 落盘后"):
        await store.append(
            session_id,
            JobCreatedEvent(
                event_id="evt_manifest_committed",
                job_id="job_manifest_committed",
                timestamp=datetime.now(UTC),
                payload=JobCreatedPayload(
                    session_id=session_id,
                    message="committed",
                    agent_id="default",
                ),
            ),
        )
    monkeypatch.setattr(TraceTurnIndex, "commit", original_commit)

    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_after_manifest",
            job_id="job_after_manifest",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id,
                message="after",
                agent_id="default",
            ),
        ),
    )

    assert [event.event_id for event in store.read_events(session_id)] == [
        "evt_manifest_committed",
        "evt_after_manifest",
    ]


@pytest.mark.asyncio
async def test_partial_semantic_trace_recovery_preserves_prior_text_delta(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_partial_after_delta"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        TextDeltaEvent(
            event_id="evt_preserved_delta",
            part_id="part_preserved_delta",
            job_id="job_preserved_delta",
            timestamp=now,
            payload=TextDeltaPayload(kind="markdown", text="keep"),
        ),
    )
    original_append = TraceEventWriter._append_bytes

    def partial_trace(file: Path, payload: bytes, *, durable: bool) -> int:
        if file.name == "events.jsonl":
            with file.open("ab") as stream:
                stream.write(payload[: max(1, len(payload) // 2)])
                stream.flush()
            raise OSError("模拟 delta 后语义事件 partial trace")
        return original_append(file, payload, durable=durable)

    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(partial_trace),
    )
    with pytest.raises(OSError, match="partial trace"):
        await store.append(
            session_id,
            JobCreatedEvent(
                event_id="evt_partial_semantic",
                job_id="job_partial_semantic",
                timestamp=now,
                payload=JobCreatedPayload(
                    session_id=session_id,
                    message="partial",
                    agent_id="default",
                ),
            ),
        )
    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(original_append),
    )

    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_after_partial",
            job_id="job_after_partial",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                message="after",
                agent_id="default",
            ),
        ),
    )

    assert [event.event_id for event in store.read_events(session_id)] == [
        "evt_preserved_delta",
        "evt_after_partial",
    ]


@pytest.mark.asyncio
async def test_latest_semantic_cursor_uses_index_without_full_trace_scan(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_indexed_cursor"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    receipt = await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_indexed_cursor",
            job_id="job_indexed_cursor",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id,
                message="cursor",
                agent_id="default",
            ),
        ),
    )

    store.ensure_cursor(session_id, "evt_indexed_cursor")
    assert (
        store._offset_after_event(
            session_id,
            store._trace_file(session_id),
            "evt_indexed_cursor",
        )
        == receipt.trace_end_offset
    )


@pytest.mark.asyncio
async def test_projected_cursor_streams_later_indexed_non_projected_event(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_projected_cursor"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_projected_created",
            job_id="job_projected_cursor",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                message="cursor",
                agent_id="default",
            ),
        ),
    )
    completed_receipt = await store.append(
        session_id,
        JobCompletedEvent(
            event_id="evt_projected_completed",
            job_id="job_projected_cursor",
            timestamp=now,
            payload=JobCompletedPayload(result="done"),
        ),
    )
    assistant_receipt = await store.append(
        session_id,
        MessageCreatedEvent(
            event_id="evt_assistant_message",
            job_id="job_projected_cursor",
            timestamp=now,
            payload=MessageCreatedPayload(
                message_id="msg_assistant",
                session_id=session_id,
                role="assistant",
                content="done",
                created_at=now,
            ),
        ),
    )

    assert (
        store._offset_after_event(
            session_id,
            store._trace_file(session_id),
            "evt_projected_completed",
        )
        == completed_receipt.trace_end_offset
    )
    assert completed_receipt.trace_end_offset < assistant_receipt.trace_end_offset

    stream = store.stream_events(session_id, "evt_projected_completed")
    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    await stream.aclose()

    assert received.event.event_id == "evt_assistant_message"


@pytest.mark.asyncio
async def test_text_delta_transport_cursor_resumes_without_trace_scan(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_delta_transport_cursor"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_delta_created",
            job_id="job_delta_cursor",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                message="delta",
                agent_id="default",
            ),
        ),
    )
    for index in range(2):
        await store.append(
            session_id,
            TextDeltaEvent(
                event_id=f"evt_delta_{index}",
                part_id="part_delta_cursor",
                job_id="job_delta_cursor",
                timestamp=now,
                payload=TextDeltaPayload(kind="markdown", text=str(index)),
            ),
        )

    first_stream = store.stream_events(session_id, "evt_delta_created")
    first_delta = await asyncio.wait_for(first_stream.asend(None), timeout=2.0)
    await first_stream.aclose()
    assert first_delta.event.event_id == "evt_delta_0"

    def reject_semantic_index(*args, **kwargs):
        raise AssertionError("transport cursor 不得访问语义 index 或扫描 Trace")

    monkeypatch.setattr(
        TraceTurnIndex,
        "trace_offset_after_event",
        reject_semantic_index,
    )
    store.ensure_cursor(session_id, first_delta.cursor)
    resumed = store.stream_events(session_id, first_delta.cursor)
    second_delta = await asyncio.wait_for(resumed.asend(None), timeout=2.0)
    await resumed.aclose()

    assert second_delta.event.event_id == "evt_delta_1"


@pytest.mark.asyncio
async def test_unindexed_raw_event_id_is_rejected_without_full_trace_scan(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_raw_unindexed_cursor"
    store, _ = _create_store(tmp_path, session_bundle_factory, session_id)
    await store.append(
        session_id,
        AgentStartEvent(
            event_id="evt_raw_unindexed",
            job_id="job_raw_unindexed",
            timestamp=datetime.now(UTC),
            payload=AgentStartPayload(message="start", agent_id="default"),
        ),
    )
    calls = 0
    original = TraceTurnIndex.trace_offset_after_event

    def bounded_index(self, event_id, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, event_id, **kwargs)

    monkeypatch.setattr(TraceTurnIndex, "trace_offset_after_event", bounded_index)

    with pytest.raises(TraceCursorGoneError, match="evt_raw_unindexed"):
        store.ensure_cursor(session_id, "evt_raw_unindexed")
    assert calls == 1


def test_legacy_index_rebuild_allows_unrecorded_interrupted_event(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_legacy_interrupted"
    store, session_dir = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    created = JobCreatedEvent(
        event_id="evt_legacy_created",
        job_id="job_legacy_interrupted",
        timestamp=now,
        payload=JobCreatedPayload(
            session_id=session_id,
            message="legacy",
            agent_id="default",
        ),
    )
    interrupted = SessionInterruptedEvent(
        event_id="evt_legacy_interrupted",
        job_id="job_legacy_interrupted",
        timestamp=now,
        payload=SessionInterruptedPayload(
            session_id=session_id,
            phase="running",
            interrupted_at=now,
        ),
    )
    text_end = TextEndEvent(
        event_id="evt_legacy_text",
        part_id="part_legacy_text",
        job_id="job_legacy_interrupted",
        timestamp=now,
        payload=TextEndPayload(kind="markdown", text="legacy result"),
    )
    traces = session_dir / "logs" / "traces"
    traces.mkdir(parents=True)
    trace_lines = [
        event.model_dump_json().encode("utf-8") + b"\n"
        for event in (created, interrupted, text_end)
    ]
    (traces / "events.jsonl").write_bytes(b"".join(trace_lines))
    (traces / "messages.jsonl").write_bytes(trace_lines[0] + trace_lines[2])

    store.ensure_turn_index(session_id)
    batch = store.read_turn_bootstrap_batch(
        session_id,
        max_events=128,
        max_bytes=64 * 1024,
    )

    assert [item.event.event_id for item in batch.events] == [
        created.event_id,
        text_end.event_id,
    ]
    assert batch.event_cursor == text_end.event_id


@pytest.mark.asyncio
async def test_legacy_prefix_is_rebuilt_after_non_semantic_append_for_resume(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_legacy_prefix_resume"
    store, session_dir = _create_store(tmp_path, session_bundle_factory, session_id)
    now = datetime.now(UTC)
    legacy_created = JobCreatedEvent(
        event_id="evt_legacy_prefix_created",
        job_id="job_legacy_prefix",
        timestamp=now,
        payload=JobCreatedPayload(
            session_id=session_id,
            message="legacy",
            agent_id="default",
        ),
    )
    later_agent_start = AgentStartEvent(
        event_id="evt_after_legacy_prefix",
        job_id="job_legacy_prefix",
        timestamp=now,
        payload=AgentStartPayload(message="resume", agent_id="default"),
    )
    traces = session_dir / "logs" / "traces"
    traces.mkdir(parents=True)
    legacy_line = legacy_created.model_dump_json().encode("utf-8") + b"\n"
    (traces / "events.jsonl").write_bytes(legacy_line)
    (traces / "messages.jsonl").write_bytes(legacy_line)

    await store.append(session_id, later_agent_start)
    unready = TraceTurnIndex(traces).snapshot()
    assert unready is not None
    assert unready.has_unindexed_prefix is True
    assert unready.event_cursor is None

    store.ensure_turn_index(session_id)
    ready = TraceTurnIndex(traces).snapshot()
    assert ready is not None
    assert ready.has_unindexed_prefix is False
    assert ready.event_cursor == legacy_created.event_id

    migration = store.capture_turn_migration_snapshot(session_id)
    assert migration.event_cursor == legacy_created.event_id
    store.ensure_cursor(session_id, migration.event_cursor)
    assert (
        store._offset_after_event(
            session_id,
            store._trace_file(session_id),
            migration.event_cursor,
        )
        == len(legacy_line)
    )

    stream = store.stream_events(session_id, migration.event_cursor)
    resumed = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    await stream.aclose()
    assert resumed.event.event_id == later_agent_start.event_id
