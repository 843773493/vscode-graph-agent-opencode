from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.lifecycle import (
    InvalidJobStatusTransitionError,
    transition_job_status,
)


@dataclass
class _Job:
    status: JobStatus
    error_message: str | None = None
    updated_at: datetime | None = None
    ended_at: datetime | None = None


def test_transition_rejects_terminal_job_restart() -> None:
    job = _Job(status=JobStatus.completed)

    with pytest.raises(
        InvalidJobStatusTransitionError,
        match="completed -> running",
    ):
        transition_job_status(job, JobStatus.running)

    assert job.status == JobStatus.completed


def test_transition_sets_terminal_metadata() -> None:
    now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
    job = _Job(status=JobStatus.running)

    changed = transition_job_status(
        job,
        JobStatus.failed,
        error_message="模型连接失败",
        now=now,
    )

    assert changed is True
    assert job.status == JobStatus.failed
    assert job.error_message == "模型连接失败"
    assert job.updated_at == now
    assert job.ended_at == now


def test_transition_same_status_is_idempotent() -> None:
    job = _Job(status=JobStatus.paused)

    changed = transition_job_status(
        job,
        JobStatus.paused,
        error_message="任务已暂停",
    )

    assert changed is False
    assert job.status == JobStatus.paused
    assert job.error_message == "任务已暂停"
    assert job.updated_at is not None
