from __future__ import annotations

from app.core.background_task_registry import ACTIVE_TASK_STATUSES
from app.schemas.internal_v2.session_resource import SessionResourceAction


def background_task_available_actions(
    status: str,
) -> list[SessionResourceAction]:
    if status == "deleted":
        return []
    if status in ACTIVE_TASK_STATUSES:
        return ["cancel", "delete"]
    return ["delete"]


def terminal_available_actions(status: str) -> list[SessionResourceAction]:
    if status == "deleted":
        return []
    if status == "running":
        return ["cancel", "delete"]
    return ["delete"]


def browser_available_actions(
    status: str,
    *,
    resource_state: str | None = None,
    has_checkpoint: bool = False,
) -> list[SessionResourceAction]:
    if status == "deleted":
        return []
    if resource_state == "discarded" and has_checkpoint and status in {
        "running",
        "lost",
    }:
        return ["resume", "delete"]
    if status == "running":
        return ["cancel", "delete"]
    return ["delete"]
