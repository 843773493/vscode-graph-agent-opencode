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
