import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.job_event_bus import JobEventBus
from app.schemas.public_v2.pending_request import PendingRequestDTO
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


@pytest.mark.asyncio
async def test_job_service_restores_accepted_pending_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    first = _service(sessions_dir)
    monkeypatch.setattr(
        first,
        "_start_job_task",
        lambda job: setattr(job, "task", _PendingTask()),
    )
    await first.start_job(
        "ses_restart",
        "active",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    await first.start_job(
        "ses_restart",
        "queued after restart",
        message_id="msg_queued",
        message_created_at="2026-07-17T00:00:01+00:00",
        pending_kind="queued",
    )

    second = _service(sessions_dir)
    started_jobs: list[str] = []
    monkeypatch.setattr(
        second,
        "_start_job_task",
        lambda job: (
            started_jobs.append(job.job_id),
            setattr(job, "task", _PendingTask()),
        ),
    )
    restored = await second.list_pending("ses_restart")

    assert restored.active_job_id is not None
    assert restored.requests == []
    assert second._jobs[restored.active_job_id].message == "queued after restart"
    assert started_jobs == [restored.active_job_id]


@pytest.mark.asyncio
async def test_restore_and_new_send_never_start_two_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    store = PendingRequestStore(sessions_dir=sessions_dir)
    now = datetime.now(timezone.utc)
    await store.save(
        "ses_restore_race",
        [
            PendingRequestDTO(
                job_id="job_restored_first",
                message_id="msg_restored_first",
                session_id="ses_restore_race",
                content="必须先执行",
                kind="queued",
                position=0,
                agent_id="default",
                message_created_at=now.isoformat(),
                created_at=now,
                updated_at=now,
            )
        ],
    )
    service = _service(sessions_dir)
    started_jobs: list[str] = []
    monkeypatch.setattr(
        service,
        "_start_job_task",
        lambda job: (
            started_jobs.append(job.job_id),
            setattr(job, "task", _PendingTask()),
        ),
    )

    _snapshot, new_dispatch = await asyncio.gather(
        service.list_pending("ses_restore_race"),
        service.start_job(
            "ses_restore_race",
            "后发送",
            message_id="msg_new",
            message_created_at=now.isoformat(),
        ),
    )

    assert started_jobs == ["job_restored_first"]
    assert new_dispatch.job_status == "queued"
    assert service._session_current_job["ses_restore_race"] == "job_restored_first"
