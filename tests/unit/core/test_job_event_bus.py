from __future__ import annotations

import asyncio

import pytest

from app.core.job_event_bus import EventType, JobEventBus


@pytest.mark.asyncio
async def test_subscribe_all_receives_all_events():
    bus = JobEventBus()
    queue = await bus.subscribe_all()

    event = await bus.publish(
        job_id="job_1",
        event_type=EventType.JOB_CREATED,
        payload={"session_id": "ses_1", "message": "hi", "agent_id": "default"},
        agent_id="test",
    )

    received = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert received.event_id == event.event_id
    assert received.type == "job_created"


@pytest.mark.asyncio
async def test_unsubscribe_all_stops_receiving():
    bus = JobEventBus()
    queue = await bus.subscribe_all()
    await bus.unsubscribe_all(queue)

    await bus.publish(
        job_id="job_1",
        event_type=EventType.JOB_CREATED,
        payload={"session_id": "ses_1", "message": "hi", "agent_id": "default"},
        agent_id="test",
    )

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.2)
