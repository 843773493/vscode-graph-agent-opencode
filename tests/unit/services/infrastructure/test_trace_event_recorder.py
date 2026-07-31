from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.abstractions.trace_event_sink import TraceAppendReceipt
from app.core.job_event_bus import EventType, JobEventBus
from app.services.business.session_turn_history import TurnHistoryProjector
from app.services.infrastructure.trace_event_recorder import TraceEventRecorder
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.infrastructure.turn_history import TurnHistoryStore


@pytest.mark.asyncio
async def test_recorder_persists_job_events(tmp_path: Path, session_bundle_factory):
    session_bundle_factory(tmp_path, "ses_1")
    bus = JobEventBus()
    store = TraceEventStore(sessions_dir=tmp_path)
    recorder = TraceEventRecorder(bus=bus, store=store)
    await recorder.start()

    try:
        await bus.publish(
            job_id="job_1",
            event_type=EventType.JOB_CREATED,
            payload={"session_id": "ses_1", "message": "hi", "agent_id": "default"},
            agent_id="test",
        )
        await bus.publish(
            job_id="job_1",
            event_type=EventType.AGENT_START,
            payload={"message": "start", "agent_id": "default"},
            agent_id="default",
        )

        events = store.read_events("ses_1")
        assert [event.type for event in events] == ["job_created", "agent_start"]
    finally:
        await recorder.stop()


@pytest.mark.asyncio
async def test_recorder_rejects_event_without_resolvable_session_id(tmp_path: Path):
    bus = JobEventBus()
    recorder = TraceEventRecorder(bus=bus, store=TraceEventStore(sessions_dir=tmp_path))
    await recorder.start()

    try:
        with pytest.raises(RuntimeError, match="缺少 session_id"):
            await bus.publish(
                job_id="job_without_session",
                event_type=EventType.AGENT_START,
                payload={"message": "start", "agent_id": "default"},
                agent_id="default",
            )
        assert await bus.list_events("job_without_session") == []
    finally:
        await recorder.stop()


@pytest.mark.asyncio
async def test_recorder_does_not_treat_session_shaped_job_id_as_session_id(tmp_path: Path):
    bus = JobEventBus()
    recorder = TraceEventRecorder(bus=bus, store=TraceEventStore(sessions_dir=tmp_path))
    await recorder.start()

    try:
        with pytest.raises(RuntimeError, match="缺少 session_id"):
            await bus.publish(
                job_id="ses_not_a_job",
                event_type=EventType.AGENT_START,
                payload={"message": "start", "agent_id": "default"},
                agent_id="default",
            )
    finally:
        await recorder.stop()


@pytest.mark.asyncio
async def test_failed_job_created_write_does_not_commit_job_session_mapping():
    class FailingSink:
        async def append(self, session_id, event):
            if event.type == EventType.JOB_CREATED:
                raise OSError(f"cannot write {session_id}")

    bus = JobEventBus()
    recorder = TraceEventRecorder(bus=bus, store=FailingSink())
    await recorder.start()

    try:
        with pytest.raises(OSError, match="cannot write ses_failed"):
            await bus.publish(
                job_id="job_failed_mapping",
                event_type=EventType.JOB_CREATED,
                payload={"session_id": "ses_failed", "message": "hi", "agent_id": "default"},
                agent_id="test",
            )
        with pytest.raises(RuntimeError, match="缺少 session_id"):
            await bus.publish(
                job_id="job_failed_mapping",
                event_type=EventType.AGENT_START,
                payload={"message": "start", "agent_id": "default"},
                agent_id="default",
            )
    finally:
        await recorder.stop()


@pytest.mark.asyncio
async def test_recorder_advances_turn_cursor_for_non_projected_event(
    tmp_path: Path,
    session_bundle_factory,
):
    session_bundle_factory(tmp_path, "ses_cursor")
    bus = JobEventBus()
    trace_store = TraceEventStore(sessions_dir=tmp_path)
    turn_store = TurnHistoryStore(tmp_path)
    recorder = TraceEventRecorder(
        bus=bus,
        store=trace_store,
        turn_projector=TurnHistoryProjector(turn_store),
    )
    await recorder.start()
    try:
        created = await bus.publish(
            job_id="job_cursor",
            event_type=EventType.JOB_CREATED,
            payload={
                "session_id": "ses_cursor",
                "message": "hi",
                "agent_id": "default",
                "message_id": "message_cursor",
            },
            agent_id="test",
        )
        delta = await bus.publish(
            job_id="job_cursor",
            event_type=EventType.TEXT_DELTA,
            payload={"part_id": "part_cursor", "kind": "markdown", "text": "x"},
            agent_id="default",
        )

        assert turn_store.event_cursor("ses_cursor") == created.event_id
        assert trace_store.read_events("ses_cursor")[-1].event_id == delta.event_id
    finally:
        await recorder.stop()


@pytest.mark.asyncio
async def test_recorder_serializes_trace_and_projection_per_session():
    class OrderedSink:
        def __init__(self) -> None:
            self.order: list[str] = []
            self.first_entered = asyncio.Event()
            self.release_first = asyncio.Event()

        async def append(self, session_id, event):
            self.order.append(f"trace:{event.job_id}")
            if event.job_id == "job_1":
                self.first_entered.set()
                await self.release_first.wait()
            return TraceAppendReceipt(
                event_id=event.event_id,
                trace_end_offset=len(self.order),
                projected_event_offset=len(self.order),
            )

    class OrderedProjector:
        def __init__(self, order: list[str]) -> None:
            self.order = order

        def record_event(self, session_id, event, *, source_offset=None):
            self.order.append(f"turn:{event.job_id}")

    bus = JobEventBus()
    sink = OrderedSink()
    recorder = TraceEventRecorder(
        bus=bus,
        store=sink,
        turn_projector=OrderedProjector(sink.order),
    )
    await recorder.start()
    try:
        first = asyncio.create_task(
            bus.publish(
                job_id="job_1",
                event_type=EventType.JOB_CREATED,
                payload={"session_id": "ses_same", "message": "1", "agent_id": "default"},
            )
        )
        await sink.first_entered.wait()
        second = asyncio.create_task(
            bus.publish(
                job_id="job_2",
                event_type=EventType.JOB_CREATED,
                payload={"session_id": "ses_same", "message": "2", "agent_id": "default"},
            )
        )
        await asyncio.sleep(0)
        assert sink.order == ["trace:job_1"]
        sink.release_first.set()
        await asyncio.gather(first, second)

        assert sink.order == [
            "trace:job_1",
            "turn:job_1",
            "trace:job_2",
            "turn:job_2",
        ]
    finally:
        await recorder.stop()


@pytest.mark.asyncio
async def test_thousands_of_text_deltas_do_not_rewrite_turn_manifest(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_many_deltas"
    session_bundle_factory(tmp_path, session_id)
    bus = JobEventBus()
    trace_store = TraceEventStore(sessions_dir=tmp_path)
    turn_store = TurnHistoryStore(tmp_path)
    recorder = TraceEventRecorder(
        bus=bus,
        store=trace_store,
        turn_projector=TurnHistoryProjector(turn_store),
    )
    await recorder.start()
    try:
        await bus.publish(
            job_id="job_many_deltas",
            event_type=EventType.JOB_CREATED,
            payload={
                "session_id": session_id,
                "message": "many deltas",
                "agent_id": "default",
            },
        )
        manifest_writes = 0
        original_write_manifest = turn_store._files.write_manifest

        def count_manifest_write(session_id: str, manifest: object) -> None:
            nonlocal manifest_writes
            manifest_writes += 1
            original_write_manifest(session_id, manifest)  # type: ignore[arg-type]

        monkeypatch.setattr(
            turn_store._files,
            "write_manifest",
            count_manifest_write,
        )
        for index in range(2_000):
            await bus.publish(
                job_id="job_many_deltas",
                event_type=EventType.TEXT_DELTA,
                payload={
                    "part_id": "part_many_deltas",
                    "kind": "markdown",
                    "text": f"{index}",
                },
                agent_id="default",
            )

        assert manifest_writes == 0
        assert turn_store.event_cursor(session_id) is not None
        assert len(trace_store.read_events(session_id, tail_limit=2)) == 2
    finally:
        await recorder.stop()
