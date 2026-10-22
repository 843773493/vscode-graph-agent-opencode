from __future__ import annotations

import asyncio
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
    TextDeltaEvent,
    TextDeltaPayload,
    TextEndEvent,
    TextEndPayload,
)
from app.services.infrastructure.trace_event_store import (
    TraceCursorGoneError,
)
from app.services.infrastructure.turn_history.trace_cursor import (
    bind_trace_cursor,
    unbind_trace_cursor,
)
from app.services.infrastructure.turn_history.trace_index import TraceTurnIndex


@pytest.mark.parametrize("kind", ["page", "stream"])
def test_public_trace_cursor_binds_session_thread_and_purpose(kind: str) -> None:
    scope = {"session_id": "ses_owner", "thread_id": "thr_owner", "kind": kind}
    cursor = bind_trace_cursor("storage-cursor", **scope)
    assert unbind_trace_cursor(cursor, **scope) == "storage-cursor"
    for changed in [
        {"session_id": "ses_other"}, {"thread_id": "thr_sibling"}, {"thread_id": None},
        {"kind": "stream" if kind == "page" else "page"},
    ]:
        with pytest.raises(TraceCursorGoneError):
            unbind_trace_cursor(cursor, **{**scope, **changed})
    for invalid in ["tc1.legacy", "ts2.!", "ts2.W10", "ts2.//8"]:
        with pytest.raises(TraceCursorGoneError):
            unbind_trace_cursor(invalid, **scope)


@pytest.mark.asyncio
async def test_latest_semantic_cursor_uses_index_without_full_trace_scan(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c30a276077c7589fb25e2fc24b2b9"
    store, _ = trace_event_store_factory(session_id)
    receipt = await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_indexed_cursor",
            job_id="job_indexed_cursor",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
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
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_019c5c7e3adc77a3b10ccaa504be3e74"
    store, _ = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_projected_created",
            job_id="job_projected_cursor",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
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
                thread_id="thr_00000000400070008000000000000002",
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
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_019c300213fe72fb8a361a7a5302067d"
    store, _ = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_delta_created",
            job_id="job_delta_cursor",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
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
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_019b973e84bf7e0f84978ddbb9456411"
    store, _ = trace_event_store_factory(session_id)
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
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c439a40ab701d85e2d2cd3d6e4348"
    store, session_dir = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    created = JobCreatedEvent(
        event_id="evt_legacy_created",
        job_id="job_legacy_interrupted",
        timestamp=now,
        payload=JobCreatedPayload(
            session_id=session_id,
            thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
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
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c4b01f682759d8fadfa0c4fd27cac"
    store, session_dir = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    legacy_created = JobCreatedEvent(
        event_id="evt_legacy_prefix_created",
        job_id="job_legacy_prefix",
        timestamp=now,
        payload=JobCreatedPayload(
            session_id=session_id,
            thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
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

    with pytest.raises(RuntimeError) as error:
        store.read_turn_bootstrap_batch(
            session_id,
            max_events=128,
            max_bytes=64 * 1024,
        )
    assert "Trace Turn bootstrap 阶段存在未索引语义事件" in str(error.value)
    assert str(traces) in str(error.value)

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
