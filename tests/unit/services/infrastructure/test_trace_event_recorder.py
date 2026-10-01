from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.job_event_bus import EventType, JobEventBus
from app.schemas.event import (
    AgentStartEvent,
    AgentStartPayload,
    JobCreatedEvent,
    JobCreatedPayload,
    JobFailedEvent,
    JobFailedPayload,
)
from app.services.infrastructure.trace_event_recorder import TraceEventRecorder
from app.services.infrastructure.trace_event_store import TraceEventStore


def _job_created(job_id: str, session_id: str) -> JobCreatedEvent:
    return JobCreatedEvent(
        event_id=f"evt_created_{job_id}",
        job_id=job_id,
        timestamp=datetime.now(UTC),
        payload=JobCreatedPayload(session_id=session_id, message="hi", agent_id="a"),
    )


def _agent_start(job_id: str) -> AgentStartEvent:
    # payload 不带 session_id：录音器必须靠 _job_sessions 映射回填。
    return AgentStartEvent(
        event_id=f"evt_start_{job_id}",
        job_id=job_id,
        timestamp=datetime.now(UTC),
        payload=AgentStartPayload(message="start", agent_id="a"),
    )


def _job_failed(job_id: str, session_id: str) -> JobFailedEvent:
    return JobFailedEvent(
        event_id=f"evt_failed_{job_id}",
        job_id=job_id,
        timestamp=datetime.now(UTC),
        payload=JobFailedPayload(error="boom", session_id=session_id),
    )


@pytest.mark.asyncio
async def test_recorder_persists_job_events(tmp_path: Path, session_bundle_factory):
    session_bundle_factory(tmp_path, "ses_019ba1bfed0773fc80049132b9745deb")
    bus = JobEventBus()
    store = TraceEventStore(sessions_dir=tmp_path)
    recorder = TraceEventRecorder(bus=bus, store=store)
    await recorder.start()

    try:
        await bus.publish(
            job_id="job_1",
            event_type=EventType.JOB_CREATED,
            payload={"session_id": "ses_019ba1bfed0773fc80049132b9745deb", "message": "hi", "agent_id": "default"},
            agent_id="test",
        )
        await bus.publish(
            job_id="job_1",
            event_type=EventType.JOB_STARTED,
            payload={"session_id": "ses_019ba1bfed0773fc80049132b9745deb"},
            agent_id="job_service",
        )
        await bus.publish(
            job_id="job_1",
            event_type=EventType.AGENT_START,
            payload={"message": "start", "agent_id": "default"},
            agent_id="default",
        )

        events = store.read_events("ses_019ba1bfed0773fc80049132b9745deb")
        assert [event.type for event in events] == [
            "job_created",
            "job_started",
            "agent_start",
        ]
    finally:
        await recorder.stop()


@pytest.mark.asyncio
async def test_recorder_resolves_lifecycle_event_without_job_created_mapping(
    tmp_path: Path,
    session_bundle_factory,
):
    session_bundle_factory(tmp_path, "ses_019b9b92ef127e83870baac3f15d7bc4")
    bus = JobEventBus()
    store = TraceEventStore(sessions_dir=tmp_path)
    recorder = TraceEventRecorder(bus=bus, store=store)
    await recorder.start()

    try:
        await bus.publish(
            job_id="job_direct",
            event_type=EventType.JOB_STARTED,
            payload={"session_id": "ses_019b9b92ef127e83870baac3f15d7bc4"},
            agent_id="job_service",
        )
        await bus.publish(
            job_id="job_direct",
            event_type=EventType.JOB_FAILED,
            payload={
                "session_id": "ses_019b9b92ef127e83870baac3f15d7bc4",
                "error": "startup timeout",
                "code": "job_startup_timeout",
            },
            agent_id="job_service",
        )

        events = store.read_events("ses_019b9b92ef127e83870baac3f15d7bc4")
        assert [event.type for event in events] == ["job_started", "job_failed"]
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
async def test_recorder_session_lock_pool_is_bounded_and_stable() -> None:
    """会话锁池对 session 数量必须有固定上界，且同一 session 恒定同一把锁。"""

    class Sink:
        async def append(self, session_id, event):
            return None

    recorder = TraceEventRecorder(bus=JobEventBus(), store=Sink())

    for index in range(5000):
        recorder._session_lock(f"ses_recorder_{index}")

    # 上界恒定：锁数量不随历史会话数增长。
    assert len(recorder._session_locks) <= 64
    # 同 session 恒定命中同一把锁（保住互斥语义）。
    assert recorder._session_lock("ses_shared") is recorder._session_lock("ses_shared")


@pytest.mark.asyncio
async def test_recorder_session_lock_serializes_same_session() -> None:
    """分片后同一 session 的临界区仍严格互斥（并发峰值恒为 1）。"""

    class Sink:
        async def append(self, session_id, event):
            return None

    recorder = TraceEventRecorder(bus=JobEventBus(), store=Sink())
    lock = recorder._session_lock("ses_serial")
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


@pytest.mark.asyncio
async def test_recorder_job_session_mapping_is_reclaimed_on_terminal_state() -> None:
    """Job 终态后 job -> session 映射应回收，上界不随历史 Job 数增长。"""

    class Sink:
        async def append(self, session_id, event):
            return None

    recorder = TraceEventRecorder(bus=JobEventBus(), store=Sink())

    for index in range(5000):
        job_id = f"job_rec_{index}"
        session_id = f"ses_rec_{index}"
        await recorder._handle_event(_job_created(job_id, session_id))
        # 终态事件携带权威 session_id，之后该 job 再无事件。
        await recorder._handle_event(_job_failed(job_id, session_id))

    # 全默认态语义：终态即回收，表上界恒为 0，与历史 Job 数无关。
    assert len(recorder._job_sessions) == 0

    # 有实质映射的活跃 Job 仍恒定命中（语义不变）。
    await recorder._handle_event(_job_created("job_live", "ses_live"))
    assert recorder._job_sessions["job_live"] == "ses_live"


@pytest.mark.asyncio
async def test_recorder_still_resolves_job_session_before_terminal_state() -> None:
    """收敛映射不得破坏「终态前依赖映射回填 session_id」的既有行为。"""

    class Sink:
        def __init__(self) -> None:
            self.appended: list[tuple[str, str]] = []

        async def append(self, session_id, event):
            self.appended.append((session_id, event.type))

    sink = Sink()
    recorder = TraceEventRecorder(bus=JobEventBus(), store=sink)

    await recorder._handle_event(_job_created("job_res", "ses_res"))
    # AGENT_START 的 payload 不带 session_id，必须靠映射回填。
    await recorder._handle_event(_agent_start("job_res"))

    assert sink.appended == [
        ("ses_res", "job_created"),
        ("ses_res", "agent_start"),
    ]
    assert recorder._job_sessions["job_res"] == "ses_res"
