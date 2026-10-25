"""Job 两个失败收口使用同一公开原因，完整原错留在诊断日志。"""

from __future__ import annotations

import asyncio
import errno
import json

import pytest

from app.core.job_event_bus import EventType
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.service import JobState
from tests.support.job_thread_fixture import job_fixture_thread_id
from tests.unit.services.business.job.ports import enqueue_job


@pytest.fixture(params=["missing", "rename", "strerror"])
def native_error(request: pytest.FixtureRequest) -> OSError:
    if request.param == "missing":
        return FileNotFoundError(errno.ENOENT, "missing", "/private/job-source")
    if request.param == "rename":
        return OSError(errno.EXDEV, "cross device", r"C:\private\job-source", None,
                       r"D:\private\job-target")
    return OSError(errno.EIO, "I/O failed at /private/job-source")


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish(self, **event: object) -> None:
        self.events.append(event)


class _FailedExecutor:
    def __init__(self, error: OSError) -> None:
        self.error = error

    async def run(self, _job: object) -> str:
        raise self.error


@pytest.mark.parametrize("boundary", ["executor", "task_callback"])
async def test_job_io_failure_has_public_reason_and_original_diagnostic(
    job_service_factory, session_lifecycle_guard, native_error: OSError,
    boundary: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    bus = _RecordingBus()
    service = job_service_factory(job_event_bus=bus, job_executor=_FailedExecutor(native_error),
                                  session_lifecycle_guard=session_lifecycle_guard)
    session_id = "session_io_failure"
    job = JobState(
        job_id="job_io_failure", session_id=session_id,
        thread_id=job_fixture_thread_id(session_id), message="验证 I/O 失败",
        message_id="msg_io_failure", message_created_at="2026-10-25T00:00:00+00:00",
        agent_id="default", status=JobStatus.queued,
        execution_binding=UserExecutionIdentity(job_id="job_io_failure", turn_id="turn_io_failure",
                                                admission_idempotency_key="adm_io_failure"),
    )
    service._jobs[job.job_id] = job
    if boundary == "task_callback":
        async def fail_before_body(*_args: object) -> None:
            raise native_error
        monkeypatch.setattr(service, "_run_job_background", fail_before_body)
        service._start_job_task(job)
        with pytest.raises(OSError) as raised:
            await job.task
        assert raised.value is native_error
        await service.drain_finalizations()
    else:
        enqueue_job(service, job, "after_turn")
        assert await service._start_next_pending(session_id, thread_id=job.thread_id)
        await asyncio.wait_for(job.task, 2)
    expected = f"执行系统 I/O 失败: error={type(native_error).__name__}, errno={native_error.errno}"
    assert job.status == JobStatus.failed
    assert (await service.get(job.job_id)).error_message == expected
    failed, = [event["payload"] for event in bus.events if event["event_type"] == EventType.JOB_FAILED]
    assert failed == {"session_id": session_id, "error": expected}
    assert "private" not in json.dumps(failed)
    assert any(record.exc_info and record.exc_info[1] is native_error for record in caplog.records)
