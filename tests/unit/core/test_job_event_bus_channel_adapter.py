"""JobEventBus 作为 `job.events/{job_id}` typed adapter 的装配证据测试（3.8-B）。

既有行为合同由 ``test_job_event_bus.py`` 原样覆盖；本文件只验证「订阅者队列、
溢出移除与短期历史确实由 EventChannelService 的 job.events/{job_id} channel
承载」这一接线事实。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.job_event_bus import JOB_EVENT_HISTORY_SIZE, EventType, JobEventBus


@pytest.mark.asyncio
async def test_publish_creates_per_job_channel_with_history() -> None:
    bus = JobEventBus()
    await bus.publish(
        job_id="job_channel_a",
        event_type=EventType.JOB_COMPLETED,
        payload={"result": "ok"},
        agent_id="test",
    )
    service = bus.event_channel_service
    assert service.channel_names == ("job.events/job_channel_a",)
    channel = service.channel("job.events/job_channel_a")
    assert channel.overflow_policy == "fail_closed"
    assert channel.spec.history_size == JOB_EVENT_HISTORY_SIZE
    assert len(channel.history) == 1
    assert channel.history[0].type == "job_completed"


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
    # fail_closed：溢出订阅者从 channel 移除，历史保持完整。
    assert queue.subscription_id not in channel.subscriber_ids
    assert channel.subscriber_ids == ()
    assert len(channel.history) == queue.maxsize + 1


@pytest.mark.asyncio
async def test_shared_event_service_reuses_channels_across_buses() -> None:
    """组合根可注入同一 EventChannelService；channel 归属按名字隔离。"""
    from app.services.infrastructure.events.event_channel_service import (
        EventChannelService,
    )

    service = EventChannelService()
    first = JobEventBus(event_service=service)
    second = JobEventBus(event_service=service)
    await first.publish(
        job_id="job_shared",
        event_type=EventType.JOB_CREATED,
        payload={"session_id": "ses_1", "message": "hi", "agent_id": "default"},
        agent_id="test",
    )
    await second.publish(
        job_id="job_shared",
        event_type=EventType.JOB_COMPLETED,
        payload={"result": "ok"},
        agent_id="test",
    )
    channel = service.channel("job.events/job_shared")
    assert len(channel.history) == 2
