from __future__ import annotations

from pathlib import Path

import pytest

from app.core.job_event_bus import EventType, JobEventBus
from app.services.infrastructure.trace_event_recorder import TraceEventRecorder
from app.services.infrastructure.trace_event_store import TraceEventStore


@pytest.mark.asyncio
async def test_recorder_persists_job_events(tmp_path: Path):
    bus = JobEventBus()
    store = TraceEventStore(logs_dir=tmp_path)
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
    recorder = TraceEventRecorder(bus=bus, store=TraceEventStore(logs_dir=tmp_path))
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
    recorder = TraceEventRecorder(bus=bus, store=TraceEventStore(logs_dir=tmp_path))
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
