from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.core.background_task_registry import BackgroundTaskHandle
from app.schemas.public_v2.job import JobDTO
from app.schemas.public_v2.session_resource import (
    SessionResourceAction,
    SessionResourceDTO,
)


class SessionResourceMapper:
    def __init__(self, *, terminal_attach_url: Callable[[str], str]) -> None:
        self._terminal_attach_url = terminal_attach_url

    def job_to_resource(
        self,
        job: JobDTO,
        *,
        available_actions: list[SessionResourceAction],
        progress_note: str | None = None,
    ) -> SessionResourceDTO:
        metadata: dict[str, object] = {
            "mode": job.mode.value,
            "entry_agent": job.entry_agent,
            "progress": job.progress,
        }
        if progress_note:
            metadata["progress_note"] = progress_note
        if job.current_step:
            metadata["current_step"] = job.current_step
        if job.error_message:
            metadata["error_message"] = job.error_message
        metadata.update(job.metadata)

        return SessionResourceDTO(
            resource_id=job.job_id,
            session_id=job.session_id,
            kind="job",
            name=f"Job / {job.entry_agent}",
            status=job.status.value,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=None,
            ended_at=job.ended_at,
            available_actions=available_actions,
            metadata=metadata,
        )

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
