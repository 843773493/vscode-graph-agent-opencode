from __future__ import annotations

import pytest

from app.core.job_event_bus import JobEventBus
from app.services.event_service import EventService, JobEventCursorGoneError


@pytest.fixture
def event_bus() -> JobEventBus:
    return JobEventBus()


@pytest.fixture
def event_service(event_bus: JobEventBus) -> EventService:
    return EventService(bus=event_bus)


@pytest.mark.anyio
async def test_job_sse_replays_after_cursor_with_transport_id(
    event_bus: JobEventBus,
    event_service: EventService,
) -> None:
    first = await event_bus.publish(
        "job-replay",
        "job_created",
        {"session_id": "session-replay", "message": "start", "agent_id": "default"},
    )
    second = await event_bus.publish("job-replay", "job_started", {})
    await event_service.ensure_cursor("job-replay", first.event_id)

    stream = event_service.stream_sse(
        "job-replay",
        after_event_id=first.event_id,
    )
    chunk = await anext(stream)
    await stream.aclose()

    assert f"id: {second.event_id}\n" in chunk
    assert "event: job.status.changed\n" in chunk


@pytest.mark.anyio
async def test_job_sse_rejects_foreign_or_missing_cursor(
    event_bus: JobEventBus,
    event_service: EventService,
) -> None:
    foreign = await event_bus.publish(
        "other-job",
        "job_created",
        {"session_id": "other-session", "message": "start", "agent_id": "default"},
    )

    with pytest.raises(JobEventCursorGoneError):
        await event_service.ensure_cursor("job-replay", foreign.event_id)
    with pytest.raises(JobEventCursorGoneError):
        await event_service.ensure_cursor("job-replay", "missing-event")
