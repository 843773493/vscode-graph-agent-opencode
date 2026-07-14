from __future__ import annotations

from app.schemas.public_v2.session_resource import SessionResourceAction


def background_task_available_actions(
    status: str,
) -> list[SessionResourceAction]:
    if status == "deleted":
        return []
    if status in {"pending", "running"}:
        return ["cancel", "delete"]
    return ["delete"]


def terminal_available_actions(status: str) -> list[SessionResourceAction]:
    if status == "deleted":
        return []
    if status == "running":
        return ["cancel", "delete"]
    return ["delete"]


def browser_available_actions(status: str) -> list[SessionResourceAction]:
    if status == "deleted":
        return []
    if status == "running":
        return ["cancel", "delete"]
    return ["delete"]
