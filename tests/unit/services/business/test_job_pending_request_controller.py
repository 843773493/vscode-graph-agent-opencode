import asyncio

import pytest

from app.core.job_event_bus import JobEventBus
from app.schemas.public_v2.common import JobStatus
from app.schemas.public_v2.pending_request import PendingRequestOrderItem
from app.services.business.job.service import JobService


class _DummyJobExecutor:
    async def run(self, job):
        return "ok"


class _DummyTask:
    def __init__(self) -> None:
        self.cancel_called = False

    def done(self) -> bool:
        return self.cancel_called

    def cancel(self) -> None:
        self.cancel_called = True


def _service() -> JobService:
    return JobService(
        job_event_bus=JobEventBus(),
        job_executor=_DummyJobExecutor(),
    )


def _prevent_background_execution(
    service: JobService,
    monkeypatch: pytest.MonkeyPatch,
    started_jobs: list[str] | None = None,
) -> None:
    def fake_start(job) -> None:
        if started_jobs is not None:
            started_jobs.append(job.job_id)
        job.task = _DummyTask()

    monkeypatch.setattr(service, "_start_job_task", fake_start)


@pytest.mark.asyncio
async def test_pending_requests_support_edit_reorder_and_remove(monkeypatch):
    service = _service()
    _prevent_background_execution(service, monkeypatch)
    session_id = "session_pending_controls"
    active = await service.start_job(
        session_id,
        "active",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    queued = await service.start_job(
        session_id,
        "queued",
        message_id="msg_queued",
        message_created_at="2026-07-17T00:00:01+00:00",
    )
    steering = await service.start_job(
        session_id,
        "steering",
        message_id="msg_steering",
        message_created_at="2026-07-17T00:00:02+00:00",
        pending_kind="steering",
    )

    snapshot = await service.list_pending(session_id)
    assert active.job_status == "running"
    assert queued.pending_kind == "queued"
    assert steering.pending_kind == "steering"
    assert [item.message_id for item in snapshot.requests] == [
        "msg_steering",
        "msg_queued",
    ]
    assert snapshot.yield_requested is True

    updated = await service.update_pending(
        session_id,
        "msg_queued",
        content="queued edited",
        attachments=[],
    )
    assert next(
        item for item in updated.requests if item.message_id == "msg_queued"
    ).content == "queued edited"

    reordered = await service.reorder_pending(
        session_id,
        [
            PendingRequestOrderItem(message_id="msg_queued", kind="steering"),
            PendingRequestOrderItem(message_id="msg_steering", kind="queued"),
        ],
    )
    assert [
        (item.message_id, item.kind) for item in reordered.requests
    ] == [
        ("msg_queued", "steering"),
        ("msg_steering", "queued"),
    ]

    after_remove = await service.remove_pending(session_id, "msg_steering")
    assert [item.message_id for item in after_remove.requests] == ["msg_queued"]


@pytest.mark.asyncio
async def test_send_immediately_promotes_target_and_cancels_active(monkeypatch):
    service = _service()
    _prevent_background_execution(service, monkeypatch)
    session_id = "session_send_immediately"
    active = await service.start_job(
        session_id,
        "active",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    first = await service.start_job(
        session_id,
        "first",
        message_id="msg_first",
        message_created_at="2026-07-17T00:00:01+00:00",
    )
    second = await service.start_job(
        session_id,
        "second",
        message_id="msg_second",
        message_created_at="2026-07-17T00:00:02+00:00",
    )

    await service.send_pending_immediately(session_id, "msg_second")

    active_job = service._jobs[active.job_id]
    assert active_job.status == JobStatus.cancelling
    assert active_job.task.cancel_called is True
    assert service._pending_queue.ids(session_id) == (
        second.job_id,
        first.job_id,
    )


@pytest.mark.asyncio
async def test_plain_cancel_keeps_pending_requests_stopped(monkeypatch):
    service = _service()
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)
    active = await service.start_job(
        "session_plain_cancel",
        "active",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    pending = await service.start_job(
        "session_plain_cancel",
        "pending",
        message_id="msg_pending",
        message_created_at="2026-07-17T00:00:01+00:00",
    )

    active_job = service._jobs[active.job_id]
    active_job.status = JobStatus.cancelled
    await service._schedule_next_job_if_needed(active_job)

    assert started_jobs == [active.job_id]
    assert service._pending_queue.ids("session_plain_cancel") == (pending.job_id,)
    assert "session_plain_cancel" not in service._session_current_job


@pytest.mark.asyncio
async def test_send_immediately_reserves_idle_session_before_concurrent_send(
    monkeypatch,
):
    service = _service()
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)
    session_id = "session_idle_immediate_race"
    active = await service.start_job(
        session_id,
        "active",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    target = await service.start_job(
        session_id,
        "立即发送目标",
        message_id="msg_target",
        message_created_at="2026-07-17T00:00:01+00:00",
    )
    active_job = service._jobs[active.job_id]
    active_job.status = JobStatus.cancelled
    await service._schedule_next_job_if_needed(active_job)

    original_promote = service._pending_requests.promote_and_reserve_if_idle
    reserved = asyncio.Event()
    release = asyncio.Event()

    async def promote_then_pause(*args, **kwargs):
        result = await original_promote(*args, **kwargs)
        reserved.set()
        await release.wait()
        return result

    monkeypatch.setattr(
        service._pending_requests,
        "promote_and_reserve_if_idle",
        promote_then_pause,
    )
    immediate_task = asyncio.create_task(
        service.send_pending_immediately(session_id, "msg_target")
    )
    await reserved.wait()
    new_dispatch = await service.start_job(
        session_id,
        "并发新消息",
        message_id="msg_new",
        message_created_at="2026-07-17T00:00:02+00:00",
    )
    release.set()
    await immediate_task

    assert new_dispatch.job_status == "queued"
    assert started_jobs == [active.job_id, target.job_id]
    assert service._session_current_job[session_id] == target.job_id


@pytest.mark.asyncio
async def test_send_immediately_rolls_back_reservation_when_persistence_fails(
    monkeypatch,
):
    service = _service()
    _prevent_background_execution(service, monkeypatch)
    session_id = "session_immediate_persist_failure"
    active = await service.start_job(
        session_id,
        "active",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    await service.start_job(
        session_id,
        "pending",
        message_id="msg_pending",
        message_created_at="2026-07-17T00:00:01+00:00",
    )
    active_job = service._jobs[active.job_id]
    active_job.status = JobStatus.cancelled
    await service._schedule_next_job_if_needed(active_job)

    async def fail_persist(_snapshot):
        raise OSError("disk full")

    monkeypatch.setattr(service._pending_requests, "persist", fail_persist)

    with pytest.raises(OSError, match="disk full"):
        await service.send_pending_immediately(session_id, "msg_pending")

    recovered_job_id = service._session_current_job[session_id]
    assert service._jobs[recovered_job_id].message_id == "msg_pending"
    assert all(
        not job.internal_reservation for job in service._jobs.values()
    )
    public_jobs = await service.list(session_id)
    assert all(not job.job_id.startswith("completed_") for job in public_jobs)


@pytest.mark.asyncio
async def test_persist_failure_with_concurrent_send_does_not_strand_queue(
    monkeypatch,
):
    service = _service()
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)
    session_id = "session_persist_concurrent_send"
    active = await service.start_job(
        session_id,
        "active",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    target = await service.start_job(
        session_id,
        "pending target",
        message_id="msg_target",
        message_created_at="2026-07-17T00:00:01+00:00",
    )
    active_job = service._jobs[active.job_id]
    active_job.status = JobStatus.cancelled
    await service._schedule_next_job_if_needed(active_job)

    persist_started = asyncio.Event()
    release_persist = asyncio.Event()
    calls = 0

    async def blocked_failure(_snapshot):
        nonlocal calls
        calls += 1
        persist_started.set()
        if calls == 1:
            await release_persist.wait()
        raise OSError("disk full")

    monkeypatch.setattr(service._pending_requests, "persist", blocked_failure)
    immediate_task = asyncio.create_task(
        service.send_pending_immediately(session_id, "msg_target")
    )
    await persist_started.wait()
    concurrent_task = asyncio.create_task(
        service.start_job(
            session_id,
            "concurrent",
            message_id="msg_concurrent",
            message_created_at="2026-07-17T00:00:02+00:00",
        )
    )
    await asyncio.sleep(0)
    release_persist.set()
    with pytest.raises(OSError, match="disk full"):
        await immediate_task
    with pytest.raises(OSError, match="disk full"):
        await concurrent_task

    assert service._session_current_job[session_id] == target.job_id
    assert started_jobs == [active.job_id, target.job_id]
    assert any(
        job.message_id == "msg_concurrent" and job.status == JobStatus.queued
        for job in service._jobs.values()
    )


@pytest.mark.asyncio
async def test_second_persist_failure_always_cleans_reservation(monkeypatch):
    service = _service()
    _prevent_background_execution(service, monkeypatch)
    session_id = "session_second_persist_failure"
    active = await service.start_job(
        session_id,
        "active",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    target = await service.start_job(
        session_id,
        "pending",
        message_id="msg_pending",
        message_created_at="2026-07-17T00:00:01+00:00",
    )
    active_job = service._jobs[active.job_id]
    active_job.status = JobStatus.cancelled
    await service._schedule_next_job_if_needed(active_job)
    calls = 0

    async def fail_second_persist(_snapshot):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second persist failed")

    monkeypatch.setattr(service._pending_requests, "persist", fail_second_persist)

    with pytest.raises(OSError, match="second persist failed"):
        await service.send_pending_immediately(session_id, "msg_pending")

    assert service._session_current_job[session_id] == target.job_id
    assert all(
        not job.internal_reservation for job in service._jobs.values()
    )
