from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast

from app.schemas.internal_v2.common import JobStatus


class JobStatusHolder(Protocol):
    status: JobStatus
    error_message: str | None
    updated_at: datetime
    ended_at: datetime | None


class InvalidJobStatusTransitionError(RuntimeError):
    """Job 生命周期状态转换不符合领域规则。"""

    def __init__(self, current: JobStatus, target: JobStatus) -> None:
        super().__init__(
            f"Job 状态转换非法: {current.value} -> {target.value}"
        )
        self.current = current
        self.target = target


TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.completed,
        JobStatus.succeeded,
        JobStatus.failed,
        JobStatus.cancelled,
        JobStatus.timed_out,
    }
)

# 失败终态子集：父任务进入这些状态后，其未执行的终端收尾提醒必须被丢弃，
# 且「失败轮次」判定只认这两个码（取消不算失败）。
FAILED_JOB_STATUSES = frozenset(
    {
        JobStatus.failed,
        JobStatus.timed_out,
    }
)

ACTIVE_JOB_STATUSES = frozenset(
    {
        JobStatus.running,
        JobStatus.streaming,
        JobStatus.waiting_input,
        JobStatus.interrupt_pending,
    }
)

# 心跳追踪子集：取消进行中的 Job 仍需继续推进 progress/current_step/updated_at，
# 直到终态写入点接管；仅把 ``cancelling`` 并入活跃集合，不改动 ACTIVE_JOB_STATUSES
# 本身（后者被 cancel 控制分支使用，语义为「可转为 cancelling 的活跃态」）。
HEARTBEAT_TRACKED_JOB_STATUSES = frozenset(
    ACTIVE_JOB_STATUSES | {JobStatus.cancelling}
)

PAUSABLE_JOB_STATUSES = frozenset(
    {
        JobStatus.running,
        JobStatus.streaming,
        JobStatus.waiting_input,
    }
)

_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.accepted: frozenset(
        {
            JobStatus.queued,
            JobStatus.running,
            JobStatus.failed,
            JobStatus.cancelled,
        }
    ),
    JobStatus.queued: frozenset(
        {
            JobStatus.running,
            JobStatus.failed,
            JobStatus.cancelled,
        }
    ),
    JobStatus.running: frozenset(
        {
            JobStatus.streaming,
            JobStatus.waiting_input,
            JobStatus.paused,
            JobStatus.interrupt_pending,
            JobStatus.cancelling,
            JobStatus.completed,
            JobStatus.succeeded,
            JobStatus.failed,
            JobStatus.cancelled,
            JobStatus.timed_out,
        }
    ),
    JobStatus.streaming: frozenset(
        {
            JobStatus.running,
            JobStatus.waiting_input,
            JobStatus.paused,
            JobStatus.interrupt_pending,
            JobStatus.cancelling,
            JobStatus.completed,
            JobStatus.succeeded,
            JobStatus.failed,
            JobStatus.cancelled,
            JobStatus.timed_out,
        }
    ),
    JobStatus.waiting_input: frozenset(
        {
            JobStatus.running,
            JobStatus.paused,
            JobStatus.interrupt_pending,
            JobStatus.cancelling,
            JobStatus.completed,
            JobStatus.succeeded,
            JobStatus.failed,
            JobStatus.cancelled,
            JobStatus.timed_out,
        }
    ),
    JobStatus.paused: frozenset(
        {
            JobStatus.running,
            JobStatus.cancelling,
            JobStatus.cancelled,
        }
    ),
    JobStatus.interrupt_pending: frozenset(
        {
            JobStatus.running,
            JobStatus.cancelling,
            JobStatus.failed,
            JobStatus.cancelled,
        }
    ),
    JobStatus.cancelling: frozenset(
        {
            JobStatus.completed,
            JobStatus.succeeded,
            JobStatus.failed,
            JobStatus.cancelled,
            JobStatus.timed_out,
        }
    ),
    JobStatus.completed: frozenset(),
    JobStatus.succeeded: frozenset(),
    JobStatus.failed: frozenset(),
    JobStatus.cancelled: frozenset(),
    JobStatus.timed_out: frozenset(),
}

_UNSET = object()


def transition_job_status(
    job: JobStatusHolder,
    target: JobStatus,
    *,
    error_message: str | None | object = _UNSET,
    now: datetime | None = None,
) -> bool:
    """校验并更新 Job 生命周期状态，返回是否发生了状态变化。"""

    current = job.status
    # TODO: Job 全量迁移到带时区时间后移除该局部例外。
    timestamp = now if now is not None else datetime.now()  # noqa: DTZ005
    if current == target:
        if error_message is not _UNSET:
            job.error_message = cast(str | None, error_message)
        job.updated_at = timestamp
        return False

    allowed = _ALLOWED_TRANSITIONS[current]
    if target not in allowed:
        raise InvalidJobStatusTransitionError(current, target)

    job.status = target
    job.updated_at = timestamp
    if error_message is not _UNSET:
        job.error_message = cast(str | None, error_message)
    if target in TERMINAL_JOB_STATUSES:
        job.ended_at = timestamp
    return True
