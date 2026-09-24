from __future__ import annotations

import asyncio

import pytest

from app.core.job_event_bus import JOB_EVENT_HISTORY_SIZE, JobEventBus
from app.services.event_service import EventService, JobEventCursorGoneError

# SSE 编码边界强制 canonical session_id（OpenSpec 2.1），事件载荷必须自带。
SESSION_ID = "ses_12345678123446788234567812345678"


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
        {"session_id": SESSION_ID, "message": "start", "agent_id": "default"},
    )
    second = await event_bus.publish(
        "job-replay", "job_started", {"session_id": SESSION_ID}
    )
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
        {"session_id": SESSION_ID, "message": "start", "agent_id": "default"},
    )

    with pytest.raises(JobEventCursorGoneError):
        await event_service.ensure_cursor("job-replay", foreign.event_id)
    with pytest.raises(JobEventCursorGoneError):
        await event_service.ensure_cursor("job-replay", "missing-event")


async def _publish_many(bus: JobEventBus, job_id: str, count: int) -> list[str]:
    event_ids: list[str] = []
    for index in range(count):
        event = await bus.publish(
            job_id,
            "job_created",
            {"session_id": SESSION_ID, "message": str(index), "agent_id": "default"},
        )
        event_ids.append(event.event_id)
    return event_ids


@pytest.mark.anyio
async def test_list_by_job_returns_whole_retention_window(
    event_bus: JobEventBus,
    event_service: EventService,
) -> None:
    """``list_by_job`` 必须返回保留窗口内全部事件，不得套用 list_events 的默认 20 条。"""
    await _publish_many(event_bus, "job-all", 25)

    assert len(await event_service.list_by_job("job-all")) == 25


@pytest.mark.anyio
async def test_stream_sse_rejects_cursor_evicted_from_window(
    event_bus: JobEventBus,
    event_service: EventService,
) -> None:
    """游标被环形历史挤掉后，``stream_sse`` 必须显式报错，不得静默重放整段历史。

    旧实现把游标直接交给 ``list_events(after=...)``：历史里查不到游标时它会
    退化成返回整段保留窗口，于是「游标之后的新事件」变成「全部 1000 条」，
    客户端收到整段重复历史且没有任何错误信号。
    """
    total = JOB_EVENT_HISTORY_SIZE + 5
    event_ids = await _publish_many(event_bus, "job-evict", total)
    # 窗口内最旧一条（下标 total-1000）之后已被挤掉：index 0 必然不在窗口内。
    evicted_cursor = event_ids[0]

    stream = event_service.stream_sse("job-evict", after_event_id=evicted_cursor)
    with pytest.raises(JobEventCursorGoneError):
        async for _ in stream:
            raise AssertionError("失效游标不得重放任何事件")


@pytest.mark.anyio
async def test_stream_sse_replays_only_events_after_live_cursor(
    event_bus: JobEventBus,
    event_service: EventService,
) -> None:
    """有效游标只重放其后的新事件，且与实时投递去重。"""
    event_ids = await _publish_many(event_bus, "job-live", 5)

    stream = event_service.stream_sse("job-live", after_event_id=event_ids[1])
    replayed = 0
    try:
        while replayed < 3:
            chunk = await asyncio.wait_for(anext(stream), timeout=1)
            assert f"id: {event_ids[2 + replayed]}\n" in chunk
            replayed += 1
    finally:
        await stream.aclose()

    assert replayed == 3
