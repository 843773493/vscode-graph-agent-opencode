from __future__ import annotations

import asyncio

import pytest

from app.core.job_event_bus import EventType, JobEventBus
from tests.support.job_thread_fixture import job_fixture_thread_id


@pytest.mark.asyncio
async def test_subscribe_registers_sink_subscriber_on_job_channel() -> None:
    bus = JobEventBus()
    queue = await bus.subscribe("job_channel_b", subscriber_kind="test_consumer")
    channel = bus.event_channel_service.channel("job.events/job_channel_b")
    assert queue.subscription_id in channel.subscriber_ids

    event = await bus.publish(
        job_id="job_channel_b",
        event_type=EventType.JOB_COMPLETED,
        payload={"result": "ok"},
        agent_id="test",
    )
    received = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert received.event_id == event.event_id

    await bus.unsubscribe("job_channel_b", queue, reason="done")
    assert queue.subscription_id not in channel.subscriber_ids
    assert bus.event_channel_service.channel_names == ()


@pytest.mark.asyncio
async def test_overflow_removes_subscriber_from_channel_registry() -> None:
    bus = JobEventBus()
    queue = await bus.subscribe("job_channel_c", subscriber_kind="test_slow_consumer")
    channel = bus.event_channel_service.channel("job.events/job_channel_c")
    for index in range(queue.maxsize + 1):
        await bus.publish(
            job_id="job_channel_c",
            event_type=EventType.JOB_COMPLETED,
            payload={"result": str(index)},
            agent_id="test",
        )
    # fail_closed：溢出订阅者移除，无人消费的通道也立即释放。
    assert queue.subscription_id not in channel.subscriber_ids
    assert channel.subscriber_ids == ()
    assert channel.history == ()
    assert bus.event_channel_service.channel_names == ()


@pytest.mark.asyncio
async def test_shared_event_service_reuses_channels_across_buses() -> None:
    """组合根可注入同一 EventChannelService；channel 归属按名字隔离。"""
    from app.services.infrastructure.events.event_channel_service import (
        EventChannelService,
    )

    service = EventChannelService()
    first = JobEventBus(event_service=service)
    second = JobEventBus(event_service=service)
    queue = await second.subscribe("job_shared", subscriber_kind="test_shared")
    await first.publish(
        job_id="job_shared",
        event_type=EventType.JOB_CREATED,
        payload={"session_id": "ses_1", "thread_id": job_fixture_thread_id("ses_1"), "message": "hi", "agent_id": "default"},
        agent_id="test",
    )
    await second.publish(
        job_id="job_shared",
        event_type=EventType.JOB_COMPLETED,
        payload={"result": "ok"},
        agent_id="test",
    )
    channel = service.channel("job.events/job_shared")
    assert channel.history == ()
    assert [(await queue.get()).type, (await queue.get()).type] == ["job_created", "job_completed"]
    await first.unsubscribe("job_shared", queue, reason="done")
    assert service.channel_names == ()
