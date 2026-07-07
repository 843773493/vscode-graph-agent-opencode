from __future__ import annotations

from app.schemas.public_v2.common import JobStatus
from app.schemas.public_v2.session_resource import SessionResourceAction


def job_available_actions(status: JobStatus) -> list[SessionResourceAction]:
    if status in {
        JobStatus.accepted,
        JobStatus.queued,
        JobStatus.running,
        JobStatus.streaming,
        JobStatus.waiting_input,
        JobStatus.interrupt_pending,
    }:
        return ["pause", "cancel"]
    if status == JobStatus.paused:
        return ["resume", "cancel"]
    if status == JobStatus.cancelling:
        return []
    return []


def job_progress_note(status: JobStatus, progress: int) -> str | None:
    if status in {
        JobStatus.accepted,
        JobStatus.queued,
        JobStatus.running,
        JobStatus.streaming,
        JobStatus.waiting_input,
        JobStatus.interrupt_pending,
    } and progress == 0:
        return "任务正在运行，当前阶段未提供细分进度；请关注状态、工具事件和最终回复。"
    return None


def background_task_available_actions(
    status: str,
) -> list[SessionResourceAction]:
    if status in {"pending", "running"}:
        return ["cancel", "delete"]
    return ["delete"]


def terminal_available_actions(status: str) -> list[SessionResourceAction]:
    if status == "deleted":
        return []
    if status == "running":
        return ["cancel", "delete"]
    return ["delete"]
