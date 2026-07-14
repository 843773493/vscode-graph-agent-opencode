from __future__ import annotations

import asyncio
import logging

import pytest

from app.core.job_event_bus import EventType, JobEventBus


@pytest.mark.asyncio
async def test_durable_listener_receives_event_before_publish_returns():
    bus = JobEventBus()
    received = []
    listener_started = asyncio.Event()
    allow_persist = asyncio.Event()

    async def record(event):
        listener_started.set()
        await allow_persist.wait()
        received.append(event)

    await bus.register_durable_listener(record)

    publish_task = asyncio.create_task(
        bus.publish(
            job_id="job_1",
            event_type=EventType.JOB_CREATED,
            payload={"session_id": "ses_1", "message": "hi", "agent_id": "default"},
            agent_id="test",
        )
    )
    await asyncio.wait_for(listener_started.wait(), timeout=1.0)
    assert not publish_task.done()

    allow_persist.set()
    event = await asyncio.wait_for(publish_task, timeout=1.0)

    assert [item.event_id for item in received] == [event.event_id]
    assert received[0].type == "job_created"


@pytest.mark.asyncio
async def test_unregister_durable_listener_stops_receiving():
    bus = JobEventBus()
    received = []

    async def record(event):
        received.append(event)

    await bus.register_durable_listener(record)
    await bus.unregister_durable_listener(record)

    await bus.publish(
        job_id="job_1",
        event_type=EventType.JOB_CREATED,
        payload={"session_id": "ses_1", "message": "hi", "agent_id": "default"},
        agent_id="test",
    )

    assert received == []


@pytest.mark.asyncio
async def test_get_event_finds_published_event():
    bus = JobEventBus()
    event = await bus.publish(
        job_id="job_1",
        event_type=EventType.JOB_COMPLETED,
        payload={"result": "ok"},
        agent_id="test",
    )

    found = await bus.get_event(event.event_id)

    assert found is not None
    assert found.event_id == event.event_id
    assert found.type == "job_completed"


@pytest.mark.asyncio
async def test_slow_transient_subscriber_is_closed_without_failing_publish(caplog):
    caplog.set_level(logging.INFO, logger="app.core.job_event_bus")
    bus = JobEventBus()
    queue = await bus.subscribe(
        "job_full",
        subscriber_kind="test_slow_consumer",
        metadata={"client_id": "client-42"},
    )
    for index in range(queue.maxsize + 1):
        event = await bus.publish(
            job_id="job_full",
            event_type=EventType.JOB_COMPLETED,
            payload={"result": str(index)},
            agent_id="test",
        )

    assert event.payload.result == str(queue.maxsize)
    with pytest.raises(RuntimeError, match="subscriber_kind=test_slow_consumer") as exc_info:
        await queue.get()
    assert exc_info.value.subscription_id == queue.subscription_id
    assert exc_info.value.job_id == "job_full"
    assert queue.metadata == {"client_id": "client-42"}
    assert len(await bus.list_events("job_full", limit=queue.maxsize + 1)) == queue.maxsize + 1
    await bus.unsubscribe(
        "job_full",
        queue,
        reason="test_consumer_closed",
    )
    log_text = caplog.text
    assert f"subscription_id={queue.subscription_id}" in log_text
    assert "subscriber_kind=test_slow_consumer" in log_text
    assert "client-42" in log_text
    assert "reason=test_consumer_closed" in log_text


@pytest.mark.asyncio
async def test_filtered_subscription_only_receives_selected_event_types():
    bus = JobEventBus()
    queue = await bus.subscribe(
        "job_filtered",
        subscriber_kind="test_agent_end_monitor",
        event_types=frozenset({EventType.AGENT_END}),
    )

    for index in range(queue.maxsize + 10):
        await bus.publish(
            job_id="job_filtered",
            event_type=EventType.AGENT_STEP,
            payload={"phase": f"step-{index}"},
            agent_id="test",
        )
    expected = await bus.publish(
        job_id="job_filtered",
        event_type=EventType.AGENT_END,
        payload={"final_text": "done", "agent_id": "test"},
        agent_id="test",
    )

    received = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert received.event_id == expected.event_id


@pytest.mark.asyncio
async def test_durable_listener_failure_prevents_history_and_transient_delivery():
    bus = JobEventBus()
    queue = await bus.subscribe(
        "job_durable_failure",
        subscriber_kind="test_durable_failure",
    )

    async def fail_to_record(event):
        raise OSError(f"cannot persist {event.event_id}")

    await bus.register_durable_listener(fail_to_record)

    with pytest.raises(OSError, match="cannot persist"):
        await bus.publish(
            job_id="job_durable_failure",
            event_type=EventType.JOB_COMPLETED,
            payload={"result": "ok"},
            agent_id="test",
        )

    assert await bus.list_events("job_durable_failure") == []
    assert queue.empty()


@pytest.mark.asyncio
async def test_slow_durable_write_only_blocks_same_job():
    bus = JobEventBus()
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()

    async def record(event):
        if event.job_id == "job_slow":
            slow_started.set()
            await release_slow.wait()

    await bus.register_durable_listener(record)
    slow_task = asyncio.create_task(
        bus.publish(
            job_id="job_slow",
            event_type=EventType.JOB_COMPLETED,
            payload={"result": "slow"},
            agent_id="test",
        )
    )
    await asyncio.wait_for(slow_started.wait(), timeout=1.0)

    fast_event = await asyncio.wait_for(
        bus.publish(
            job_id="job_fast",
            event_type=EventType.JOB_COMPLETED,
            payload={"result": "fast"},
            agent_id="test",
        ),
        timeout=1.0,
    )
    assert fast_event.payload.result == "fast"
    assert not slow_task.done()

    release_slow.set()
    await asyncio.wait_for(slow_task, timeout=1.0)


@pytest.mark.asyncio
async def test_concurrent_events_for_same_job_keep_publish_order():
    bus = JobEventBus()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    recorded_results: list[str] = []

    async def record(event):
        if event.payload.result == "first":
            first_started.set()
            await release_first.wait()
        recorded_results.append(event.payload.result)

    await bus.register_durable_listener(record)
    first_task = asyncio.create_task(
        bus.publish(
            job_id="job_ordered",
            event_type=EventType.JOB_COMPLETED,
            payload={"result": "first"},
            agent_id="test",
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    second_task = asyncio.create_task(
        bus.publish(
            job_id="job_ordered",
            event_type=EventType.JOB_COMPLETED,
            payload={"result": "second"},
            agent_id="test",
        )
    )
    await asyncio.sleep(0)
    assert not second_task.done()

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert recorded_results == ["first", "second"]
