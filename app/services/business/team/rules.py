from __future__ import annotations

from app.schemas.internal_v2.team import (
    TeamBoardDTO,
    TeamMemberDTO,
    TeamTaskDTO,
    TeamTaskStatus,
)

TASK_TRANSITIONS: dict[TeamTaskStatus, frozenset[TeamTaskStatus]] = {
    "queued": frozenset({"in_progress", "failed", "cancelled"}),
    "in_progress": frozenset({"blocked", "completed", "failed", "cancelled"}),
    "blocked": frozenset({"in_progress", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
# 任务产生结论的状态：落到这些状态即要求填写 summary，并通知协调者。
# 两处判定（结论必填、协调者通知）共用同一闭集，故只在此定义一次。
RESOLUTION_STATUSES = frozenset({"blocked", "completed", "failed"})


def find_member(
    board: TeamBoardDTO,
    thread_id: str,
    *,
    required: bool = True,
) -> TeamMemberDTO | None:
    for member in board.members:
        if member.thread_id == thread_id and member.status != "cancelled":
            return member
    if required:
        raise PermissionError(f"Thread 不是团队成员: {thread_id}")
    return None


def require_active_member(board: TeamBoardDTO, thread_id: str) -> TeamMemberDTO:
    member = find_member(board, thread_id)
    if member is None or member.status != "active":
        raise PermissionError(
            f"团队成员尚未激活: team_id={board.team_id} thread_id={thread_id}"
        )
    return member


def require_coordinator(board: TeamBoardDTO, thread_id: str) -> None:
    require_active_member(board, thread_id)
    if board.coordinator_thread_id != thread_id:
        raise PermissionError("只有团队协调者可以执行此操作")


def find_task(board: TeamBoardDTO, task_id: str) -> TeamTaskDTO:
    for task in board.tasks:
        if task.task_id == task_id:
            return task
    raise ValueError(f"团队任务不存在: {task_id}")


def required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} 不能为空")
    return normalized
