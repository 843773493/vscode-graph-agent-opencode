from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.core.background_task_registry import BackgroundTaskHandle
from app.schemas.public_v2.session_resource import (
    SessionResourceAction,
    SessionResourceDTO,
)


class SessionResourceMapper:
    def __init__(
        self,
        *,
        terminal_attach_url: Callable[[str], str],
        browser_attach_url: Callable[[str], str],
    ) -> None:
        self._terminal_attach_url = terminal_attach_url
        self._browser_attach_url = browser_attach_url

    def background_task_to_resource(
        self,
        handle: BackgroundTaskHandle,
        *,
        available_actions: list[SessionResourceAction],
    ) -> SessionResourceDTO:
        updated_at = handle.ended_at or handle.started_at or handle.created_at
        return SessionResourceDTO(
            resource_id=handle.task_id,
            session_id=handle.session_id,
            kind="background_task",
            name=handle.task_name,
            status=handle.status,
            created_at=handle.created_at,
            updated_at=updated_at,
            started_at=handle.started_at,
            ended_at=handle.ended_at,
            available_actions=available_actions,
            metadata=handle.metadata,
        )

    def terminal_to_resource(
        self,
        terminal: dict[str, object],
        *,
        available_actions: list[SessionResourceAction],
    ) -> SessionResourceDTO:
        terminal_id = str(terminal["terminal_id"])
        session_id = str(terminal["session_id"])
        status = str(terminal.get("status") or "unknown")
        created_at = parse_datetime(terminal.get("created_at"), "created_at", terminal_id)
        updated_at = parse_datetime(terminal.get("updated_at"), "updated_at", terminal_id)
        started_at = parse_optional_datetime(terminal.get("started_at"), "started_at", terminal_id)
        ended_at = parse_optional_datetime(terminal.get("ended_at"), "ended_at", terminal_id)

        metadata = {
            "cwd": terminal.get("cwd"),
            "command": terminal.get("last_command"),
            "shell_command": terminal.get("command"),
            "command_status": terminal.get("last_command_status"),
            "command_exit_code": terminal.get("last_command_exit_code"),
            "command_started_at": terminal.get("last_command_started_at"),
            "command_completed_at": terminal.get("last_command_completed_at"),
            "last_input": terminal.get("last_input"),
            "last_input_source": terminal.get("last_input_source"),
            "last_input_at": terminal.get("last_input_at"),
            "os_pid": terminal.get("os_pid"),
            "process_group_id": terminal.get("process_group_id"),
            "process_session_id": terminal.get("process_session_id"),
            "release_reason": terminal.get("release_reason"),
            "attach_url": terminal.get("attach_url") or self._terminal_attach_url(terminal_id),
            "client_count": terminal.get("client_count"),
            "sequence": terminal.get("sequence"),
        }
        if terminal.get("historical_only") is True:
            metadata["resource_source"] = "历史记录"
            metadata["status_note"] = "终端管理器中已无该终端，可能已删除或由旧版本清理。"
            metadata["historical_status"] = terminal.get("historical_status")
        elif status == "lost":
            metadata["status_note"] = "终端进程已断开 (lost)，通常是终端管理器重启后无法重新 attach。"
        elif terminal.get("release_reason") == "terminal_manager_startup_cleanup":
            metadata["status_note"] = "终端管理器启动时已自动释放上次遗留的终端进程。"
        elif str(terminal.get("release_reason") or "").startswith("terminal_manager_"):
            metadata["status_note"] = "终端管理器退出时已自动释放该终端进程。"
        elif status == "deleted":
            metadata["status_note"] = "终端已删除 (deleted)，仅保留历史记录，当前不可 attach。"

        return SessionResourceDTO(
            resource_id=terminal_id,
            session_id=session_id,
            kind="terminal",
            name=f"终端 / {terminal_id}",
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            started_at=started_at,
            ended_at=ended_at,
            available_actions=available_actions,
            metadata=metadata,
        )

    def browser_to_resource(
        self,
        browser: dict[str, object],
        *,
        available_actions: list[SessionResourceAction],
    ) -> SessionResourceDTO:
        browser_id = str(browser["browser_id"])
        session_id = str(browser["session_id"])
        status = str(browser.get("status") or "unknown")
        created_at = parse_datetime(browser.get("created_at"), "created_at", browser_id)
        updated_at = parse_datetime(browser.get("updated_at"), "updated_at", browser_id)
        started_at = parse_optional_datetime(browser.get("started_at"), "started_at", browser_id)
        ended_at = parse_optional_datetime(browser.get("ended_at"), "ended_at", browser_id)

        metadata = {
            "page_id": browser.get("page_id") or browser_id,
            "url": browser.get("url"),
            "title": browser.get("title"),
            "viewport": browser.get("viewport"),
            "attach_url": browser.get("attach_url") or self._browser_attach_url(browser_id),
            "client_count": browser.get("client_count"),
            "sequence": browser.get("sequence"),
            "pending_dialog": browser.get("pending_dialog"),
            "pending_file_chooser": browser.get("pending_file_chooser"),
            "release_reason": browser.get("release_reason"),
            "error_message": browser.get("error_message"),
        }
        if status == "lost":
            metadata["status_note"] = "浏览器管理器重启后无法重新 attach 旧页面，请重新打开页面。"
        elif status == "closed":
            metadata["status_note"] = "浏览器页面已关闭，仅保留历史记录，当前不可 attach。"
        elif status == "deleted":
            metadata["status_note"] = "浏览器页面已删除，仅保留历史记录，当前不可 attach。"
        elif status == "failed":
            metadata["status_note"] = "浏览器页面启动或导航失败，仅保留失败记录。"

        return SessionResourceDTO(
            resource_id=browser_id,
            session_id=session_id,
            kind="browser",
            name=f"浏览器 / {browser.get('title') or browser_id}",
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            started_at=started_at,
            ended_at=ended_at,
            available_actions=available_actions,
            metadata=metadata,
        )


def parse_datetime(value: object, field_name: str, terminal_id: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"terminal {terminal_id} 缺少时间字段: {field_name}")
    return normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))


def parse_optional_datetime(
    value: object,
    field_name: str,
    terminal_id: str,
) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"terminal {terminal_id} 时间字段格式错误: {field_name}")
    return normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
