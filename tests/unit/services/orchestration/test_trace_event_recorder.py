from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.job_event_bus import EventType, JobEventBus
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.orchestration.trace_event_recorder import TraceEventRecorder


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

        await asyncio.sleep(0.2)
        events = store.read_events("ses_1")
        assert [e.type for e in events] == ["job_created", "agent_start"]
    finally:
        await recorder.stop()


@pytest.mark.asyncio
async def test_recorder_resolves_session_id_from_job_mapping(tmp_path: Path):
    bus = JobEventBus()
    store = TraceEventStore(logs_dir=tmp_path)
    recorder = TraceEventRecorder(bus=bus, store=store)
    await recorder.start()

    try:
        await bus.publish(
            job_id="job_2",
            event_type=EventType.JOB_CREATED,
            payload={"session_id": "ses_2", "message": "hi", "agent_id": "default"},
            agent_id="test",
        )
        await bus.publish(
            job_id="job_2",
            event_type=EventType.AGENT_START,
            payload={"message": "start", "agent_id": "default"},
            agent_id="default",
        )

        await asyncio.sleep(0.2)
        events = store.read_events("ses_2")
        assert len(events) == 2
        assert events[0].type == "job_created"
        assert events[1].type == "agent_start"
    finally:
        await recorder.stop()
