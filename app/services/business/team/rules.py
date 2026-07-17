from __future__ import annotations

from app.schemas.public_v2.team import (
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
SUMMARY_REQUIRED_STATUSES = {"blocked", "completed", "failed"}


def find_member(
    board: TeamBoardDTO,
    session_id: str,
    *,
    required: bool = True,
) -> TeamMemberDTO | None:
    for member in board.members:
        if member.session_id == session_id and member.status != "removed":
            return member
    if required:
        raise PermissionError(f"Session 不是团队成员: {session_id}")
    return None


def require_active_member(board: TeamBoardDTO, session_id: str) -> TeamMemberDTO:
    member = find_member(board, session_id)
    if member is None or member.status != "active":
        raise PermissionError(
            f"团队成员尚未激活: team_id={board.team_id} session_id={session_id}"
        )
    return member


def require_coordinator(board: TeamBoardDTO, session_id: str) -> None:
    require_active_member(board, session_id)
    if board.coordinator_session_id != session_id:
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
