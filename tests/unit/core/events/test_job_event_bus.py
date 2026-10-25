from __future__ import annotations

import asyncio
import logging

import pytest
from pydantic import ValidationError

from app.core.job_event_bus import EventType, JobEventBus
from app.core.resource_uri import session_artifact_uri
from app.schemas.event import Event
from tests.support.job_thread_fixture import job_fixture_thread_id


@pytest.fixture
def tool_output_payload() -> dict[str, object]:
    uri = session_artifact_uri(
        "workspace_1", "ses_019bd0aa8bb2784aaadbe51563ee17f0", "tool-results",
        "tool_" + "a" * 24 + ".txt",
        gateway_authority="gateway_" + "b" * 32,
    )
    return {
        "path": uri, "read_path": uri, "tool_name": "large_tool",
        "tool_call_id": "call_large", "byte_count": 2048, "line_count": 80,
        "content_sha256": "c" * 64,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["path", "read_path"])
@pytest.mark.parametrize("invalid", [
    "/private/workspace/.boxteam/sessions/tool.txt",
    r"C:\Users\local\.boxteam\tool.txt",
    "tool-results/tool.txt",
    "boxteam://session/ses_1/resources/artifacts/tool-results/tool.txt",
    "boxteam://workspace/workspace_1/resources/files/root/tool.txt",
    "boxteam://workspace/workspace_1/resources/session/ses_019bd0aa8bb2784aaadbe51563ee17f0/attachments/att_" + "a" * 32,
])
async def test_tool_output_rejects_non_result_uri_before_delivery(
    tool_output_payload: dict[str, object], field: str, invalid: str,
) -> None:
    bus = JobEventBus()
    queue = await bus.subscribe("job_invalid_output", subscriber_kind="test_tool_output")
    received: list[Event] = []

    async def record(event: Event) -> None:
        received.append(event)

    await bus.register_durable_listener(record)
    with pytest.raises(ValidationError) as failure:
        await bus.publish(
            job_id="job_invalid_output", event_type=EventType.TOOL_CALL_END,
            payload={"part_id": "part_output",
                "execution_id": "execution_1", "tool_call_id": "call_large",
                "tool_name": "large_tool", "tool_output": {**tool_output_payload, field: invalid},
            },
        )
    assert failure.value.errors(include_input=False)[0]["loc"] == ("tool_output", field)
    assert received == []
    assert await bus.list_events("job_invalid_output") == []
    assert queue.empty()


@pytest.mark.asyncio
async def test_tool_output_uri_round_trips_without_resolving_target(
    tool_output_payload: dict[str, object],
) -> None:
    bus = JobEventBus()
    queue = await bus.subscribe("job_output", subscriber_kind="test_tool_output")
    received: list[Event] = []

    async def record(event: Event) -> None:
        received.append(event)

    await bus.register_durable_listener(record)
    event = await bus.publish(
        job_id="job_output", event_type=EventType.TOOL_CALL_END,
        payload={"part_id": "part_output", "execution_id": "execution_1", "tool_call_id": "call_large",
                 "tool_name": "large_tool", "tool_output": tool_output_payload},
    )
    expected = {"type": "tool_output", "truncated": True, **tool_output_payload}
    assert event.payload.tool_output.model_dump() == expected
    assert received == [event]
    assert (await bus.list_events("job_output"))[0].payload.tool_output.model_dump() == expected
    assert (await queue.get()).model_dump(mode="json")["payload"]["tool_output"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_fields", [{}, {"thread_id": ""}, {"thread_id": None}])
async def test_job_created_rejects_missing_thread_before_delivery(
    thread_fields: dict[str, str | None],
) -> None:
    bus = JobEventBus()
    received: list[Event] = []

    async def record(event: Event) -> None:
        received.append(event)

    await bus.register_durable_listener(record)
    with pytest.raises(ValidationError, match="thread_id"):
        await bus.publish(
            job_id="job_missing_thread",
            event_type=EventType.JOB_CREATED,
            payload={
                "session_id": "ses_1", "message": "hi", "agent_id": "default",
                **thread_fields,
            },
        )

    assert received == []
    assert await bus.list_events("job_missing_thread") == []


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
            payload={"session_id": "ses_1", "thread_id": job_fixture_thread_id("ses_1"), "message": "hi", "agent_id": "default"},
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
        payload={"session_id": "ses_1", "thread_id": job_fixture_thread_id("ses_1"), "message": "hi", "agent_id": "default"},
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


@pytest.mark.asyncio
async def test_list_events_rejects_non_positive_limit():
    """``limit<=0`` 必须显式失败，不得静默返回全量或错误子集。

    ``events[-limit:]`` 在 ``limit=0`` 时退化为 ``events[0:]``（返回全部
    历史），``limit<0`` 时退化为 ``events[abs(limit):]``（丢掉最旧的若干条）。
    两者都是静默的错误结果；HTTP 路由 ``GET /jobs/{id}/events`` 的
    ``limit`` 没有下限约束，调用方传入 0 会拿到整段历史。
    """
    bus = JobEventBus()
    for index in range(5):
        await bus.publish(
            job_id="job_limit",
            event_type=EventType.JOB_COMPLETED,
            payload={"result": str(index)},
            agent_id="test",
        )

    assert len(await bus.list_events("job_limit", limit=2)) == 2
    with pytest.raises(ValueError, match="limit"):
        await bus.list_events("job_limit", limit=0)
    with pytest.raises(ValueError, match="limit"):
        await bus.list_events("job_limit", limit=-1)


@pytest.mark.asyncio
async def test_publish_lock_table_stays_bounded_across_many_jobs():
    """per-job publish 锁表对 job 数量必须有固定上界。

    JobEventBus 是容器级长驻单例：若每个出现过的 job_id 都永久留下一把
    ``asyncio.Lock``，长驻进程内存会随历史 Job 数无界增长（与 e18e4c48 /
    bdd6ecd1 同源）。上界必须与历史 Job 数无关。
    """
    bus = JobEventBus()
    for index in range(5000):
        await bus.publish(
            job_id="job_bounded_" + str(index),
            event_type=EventType.JOB_COMPLETED,
            payload={"result": str(index)},
            agent_id="test",
        )

    # 固定上界（分片数量级），不依赖内部常量即可断言有界性。
    assert len(bus._job_publish_locks) <= 64


@pytest.mark.asyncio
async def test_publish_lock_is_stable_and_serializes_same_job():
    """分片锁池：同一 job 恒得同一把锁，同 job 发布仍严格串行。"""
    bus = JobEventBus()

    assert bus._publish_lock_for("job_shared") is bus._publish_lock_for("job_shared")

    lock = bus._publish_lock_for("job_shared")
    concurrent = 0
    peak = 0

    async def worker() -> None:
        nonlocal concurrent, peak
        async with lock:
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1

    await asyncio.gather(*(worker() for _ in range(20)))
    assert peak == 1
