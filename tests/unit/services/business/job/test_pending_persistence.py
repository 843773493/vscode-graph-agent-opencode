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


def _service(sessions_dir: Path) -> JobService:
    return JobService(
        job_event_bus=JobEventBus(),
        job_executor=_UnusedExecutor(),
        pending_request_store=PendingRequestStore(sessions_dir=sessions_dir),
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
    )


@pytest.mark.asyncio
async def test_job_service_restores_only_messages_still_in_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "ses_restart")
    store = PendingRequestStore(sessions_dir=sessions_dir)
    await store.save(
        "ses_restart",
        [
            _request(
                "ses_restart",
                job_id="job_first",
                message_id="msg_first",
                sequence=1,
            ),
            _request(
                "ses_restart",
                job_id="job_second",
                message_id="msg_second",
                sequence=2,
            ),
        ],
    )

    service = _service(sessions_dir)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)

    restored = await service.list_pending("ses_restart")

    assert restored.active_job_id == "job_first"
    assert [item.message_id for item in restored.requests] == ["msg_second"]
    assert restored.requests[0].status == "queued"
    assert started_jobs == ["job_first"]


@pytest.mark.asyncio
async def test_restore_and_new_send_keep_one_session_fifo_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "ses_restore_race")
    store = PendingRequestStore(sessions_dir=sessions_dir)
    await store.save(
        "ses_restore_race",
        [_request(
            "ses_restore_race",
            job_id="job_restored_first",
            message_id="msg_restored_first",
            sequence=1,
        )],
    )
    service = _service(sessions_dir)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)

    _snapshot, new_dispatch = await asyncio.gather(
        service.list_pending("ses_restore_race"),
        service.start_job(
            "ses_restore_race",
            "后发送",
            message_id="msg_new",
            message_created_at=datetime.now(UTC).isoformat(),
        ),
    )

    assert started_jobs == ["job_restored_first"]
    assert new_dispatch.job_status == "queued"
    assert service._session_current_job["ses_restore_race"] == "job_restored_first"
    assert service._pending_queue.ids("ses_restore_race") == (new_dispatch.job_id,)


@pytest.mark.asyncio
async def test_dispatch_removes_started_head_from_persistent_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_dispatch_persistence"
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

    service = _service(sessions_dir)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)

    pending = await service.list_pending(session_id)

    assert pending.active_job_id == "job_started"
    assert pending.requests == []
    assert started_jobs == ["job_started"]
    assert await store.load(session_id) == []
