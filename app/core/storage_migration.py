from __future__ import annotations

import shutil
from pathlib import Path


def _move_legacy_path(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if target.exists():
        raise FileExistsError(f"迁移目标已存在，拒绝覆盖: source={source} target={target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def _quarantine_legacy_path(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if source.is_dir() and not any(source.iterdir()):
        source.rmdir()
        return
    _move_legacy_path(source, target)


def migrate_workspace_storage_layout(
    *,
    boxteam_root: Path,
    sessions_root: Path,
) -> None:
    """把当前工作区旧的分散会话数据一次性迁入 sessions/{session_id}/。"""
    if not sessions_root.exists():
        return

    for session_dir in sessions_root.iterdir():
        if not session_dir.is_dir() or not (session_dir / "session.json").is_file():
            continue
        session_id = session_dir.name
        _move_legacy_path(
            boxteam_root / "checkpoints" / session_id,
            session_dir / "checkpoints",
        )
        _move_legacy_path(
            boxteam_root / "logs" / "llm_requests" / session_id,
            session_dir / "logs" / "llm_requests",
        )
        _move_legacy_path(
            boxteam_root / "logs" / "traces" / f"trace_{session_id}.jsonl",
            session_dir / "logs" / "traces" / "events.jsonl",
        )
        _move_legacy_path(
            boxteam_root / "logs" / "traces" / f"trace_message_{session_id}.jsonl",
            session_dir / "logs" / "traces" / "messages.jsonl",
        )
        _move_legacy_path(
            boxteam_root / "background_tasks" / f"{session_id}.json",
            session_dir / "resources" / "background_tasks.json",
        )
        _move_legacy_path(
            boxteam_root / "conversation_history" / f"{session_id}.md",
            session_dir / "context" / "history.md",
        )

    orphaned_root = boxteam_root / "orphaned"
    for source, name in (
        (boxteam_root / "checkpoints", "legacy-checkpoints"),
        (boxteam_root / "logs" / "llm_requests", "legacy-llm-requests"),
        (boxteam_root / "logs" / "traces", "legacy-traces"),
        (boxteam_root / "background_tasks", "legacy-background-tasks"),
        (boxteam_root / "conversation_history", "legacy-conversation-history"),
    ):
        _quarantine_legacy_path(source, orphaned_root / name)


def migrate_user_storage_layout(
    *,
    home: Path,
    boxteam_home: Path,
    default_workspace_root: Path,
) -> None:
    """把旧的 ~/.boxteam 与默认工作区 Gateway 数据迁入统一全局目录。"""
    config_root = boxteam_home / "config"
    legacy_config_root = home / ".boxteam"
    for file_name in ("boxteam.jsonc", "config.schema.jsonc"):
        _move_legacy_path(legacy_config_root / file_name, config_root / file_name)
    _move_legacy_path(
        default_workspace_root / ".boxteam" / "gateway",
        boxteam_home / "state" / "gateway",
    )
    legacy_ui_settings = legacy_config_root / "web_ui_settings.json"
    if legacy_ui_settings.exists():
        current_ui_settings = boxteam_home / "state" / "gateway" / "web_ui_settings.json"
        ui_target = (
            boxteam_home / "state" / "migrated" / "legacy_web_ui_settings.json"
            if current_ui_settings.exists()
            else current_ui_settings
        )
        _move_legacy_path(legacy_ui_settings, ui_target)
