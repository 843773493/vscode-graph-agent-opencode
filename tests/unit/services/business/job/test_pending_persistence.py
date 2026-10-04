import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.job_event_bus import JobEventBus
from app.schemas.internal_v2.pending_request import PendingRequestDTO
from app.services.business.job.service import JobService
from app.services.infrastructure.pending_request_store import PendingRequestStore


class _UnusedExecutor:
    async def run(self, job):
        raise AssertionError(f"恢复待处理消息不应执行 Job: {job.job_id}")


class _PendingTask:
    def done(self) -> bool:
        return False

    def add_done_callback(self, _callback) -> None:
        return None


def _service(sessions_dir: Path, session_lifecycle_guard) -> JobService:
    return JobService(
        job_event_bus=JobEventBus(),
        job_executor=_UnusedExecutor(),
        pending_request_store=PendingRequestStore(sessions_dir=sessions_dir),
        session_lifecycle_guard=session_lifecycle_guard,
    )


def _prevent_background_execution(
    service: JobService,
    monkeypatch: pytest.MonkeyPatch,
    started_jobs: list[str] | None = None,
) -> None:
    def fake_start(job) -> None:
        if started_jobs is not None:
            started_jobs.append(job.job_id)
        job.task = _PendingTask()

    monkeypatch.setattr(service, "_start_job_task", fake_start)


def _request(
    session_id: str,
    *,
    job_id: str,
    message_id: str,
    sequence: int,
    gateway_id: str | None = "gateway_test",
    request_id: str | None = "request_test",
) -> PendingRequestDTO:
    now = datetime.now(UTC)
    return PendingRequestDTO(
        job_id=job_id,
        message_id=message_id,
        session_id=session_id,
        content=message_id,
        delivery_policy="after_turn",
        enqueue_sequence=sequence,
        position=sequence - 1,
        agent_id="default",
        message_created_at=now.isoformat(),
        created_at=now,
        updated_at=now,
        snapshot_version=1,
        gateway_id=gateway_id,
        request_id=request_id,
    )


@pytest.mark.asyncio
async def test_job_service_restores_only_messages_still_in_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "ses_019c018e721e70b38f769ca61010e13a")
    store = PendingRequestStore(sessions_dir=sessions_dir)
    await store.save(
        "ses_019c018e721e70b38f769ca61010e13a",
        [
            _request(
                "ses_019c018e721e70b38f769ca61010e13a",
                job_id="job_first",
                message_id="msg_first",
                sequence=1,
            ),
            _request(
                "ses_019c018e721e70b38f769ca61010e13a",
                job_id="job_second",
                message_id="msg_second",
                sequence=2,
            ),
        ],
    )

    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)

    restored = await service.list_pending("ses_019c018e721e70b38f769ca61010e13a")

    assert restored.active_job_id == "job_first"
    assert [item.message_id for item in restored.requests] == ["msg_second"]
    assert restored.requests[0].status == "queued"
    assert started_jobs == ["job_first"]


@pytest.mark.asyncio
async def test_restore_and_new_send_keep_one_session_fifo_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "ses_019b8605a35b7fd6829e9dfec660d38c")
    store = PendingRequestStore(sessions_dir=sessions_dir)
    await store.save(
        "ses_019b8605a35b7fd6829e9dfec660d38c",
        [
            _request(
                "ses_019b8605a35b7fd6829e9dfec660d38c",
                job_id="job_restored_first",
                message_id="msg_restored_first",
                sequence=1,
            )
        ],
    )
    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)

    _snapshot, new_dispatch = await asyncio.gather(
        service.list_pending("ses_019b8605a35b7fd6829e9dfec660d38c"),
        service.start_job(
            "ses_019b8605a35b7fd6829e9dfec660d38c",
            "后发送",
            message_id="msg_new",
            message_created_at=datetime.now(UTC).isoformat(),
        ),
    )

    assert started_jobs == ["job_restored_first"]
    assert new_dispatch.job_status == "queued"
    assert (
        service._session_current_job["ses_019b8605a35b7fd6829e9dfec660d38c"]
        == "job_restored_first"
    )
    assert service._pending_queue.ids("ses_019b8605a35b7fd6829e9dfec660d38c") == (
        new_dispatch.job_id,
    )


@pytest.mark.asyncio
async def test_dispatch_removes_started_head_from_persistent_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c5edf70037e1791483733a8865f97"
    session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    await store.save(
        session_id,
        [
            _request(
                session_id,
                job_id="job_started",
                message_id="msg_started",
                sequence=1,
            ),
        ],
    )

    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)

    pending = await service.list_pending(session_id)

    assert pending.active_job_id == "job_started"
    assert pending.requests == []
    assert started_jobs == ["job_started"]
    assert await store.load(session_id) == []


@pytest.mark.asyncio
async def test_restored_pending_job_carries_persisted_gateway_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    """磁盘恢复的待处理 Job 必须沿用创建时持久化的真实 gateway_id。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019b990a9cce734bbccc60ec5321f320"
    session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    await store.save(
        session_id,
        [
            _request(
                session_id,
                job_id="job_head",
                message_id="msg_head",
                sequence=1,
                gateway_id="gateway_restored1234",
            ),
            _request(
                session_id,
                job_id="job_tail",
                message_id="msg_tail",
                sequence=2,
                gateway_id="gateway_restored1234",
            ),
        ],
    )

    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard)
    _prevent_background_execution(service, monkeypatch)

    restored = await service.list_pending(session_id)

    assert restored.active_job_id == "job_head"
    assert service._jobs["job_head"].gateway_id == "gateway_restored1234"
    assert service._jobs["job_tail"].gateway_id == "gateway_restored1234"


@pytest.mark.asyncio
async def test_restored_pending_job_carries_persisted_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    """磁盘恢复的待处理 Job 必须沿用创建请求的权威 request_id，不补造第二个。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_00000000f00070008000000000a1b2c3"
    session_bundle_factory(sessions_dir, session_id)
    store = PendingRequestStore(sessions_dir=sessions_dir)
    await store.save(
        session_id,
        [
            _request(
                session_id,
                job_id="job_head",
                message_id="msg_head",
                sequence=1,
                request_id="request_restored1234",
            ),
        ],
    )

    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard)
    _prevent_background_execution(service, monkeypatch)

    restored = await service.list_pending(session_id)

    assert restored.active_job_id == "job_head"
    assert service._jobs["job_head"].request_id == "request_restored1234"
