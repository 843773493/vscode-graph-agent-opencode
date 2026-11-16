from __future__ import annotations

from app.domain.background_work import ACTIVE_BACKGROUND_TASK_STATUSES
from app.schemas.internal_v2.session_resource import SessionResourceAction


def background_task_available_actions(
    status: str,
) -> list[SessionResourceAction]:
    if status == "reconcile_required":
        return ["reconcile"]
    if status == "deleted":
        return []
    if status in ACTIVE_BACKGROUND_TASK_STATUSES:
        return ["cancel", "delete"]
    return ["delete"]
