from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path


def _move_legacy_path(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if target.exists():
        raise FileExistsError(
            f"迁移目标已存在，拒绝覆盖: source={source} target={target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def _quarantine_legacy_path(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if source.is_dir() and not any(source.iterdir()):
        source.rmdir()
        return
    _move_legacy_path(source, target)


def _normalize_legacy_trace_file(
    *,
    trace_file: Path,
    backup_file: Path,
) -> int:
    """将旧版 Trace 中不带时区的顶层 timestamp 解释为 UTC。"""
    lines = trace_file.read_text(encoding="utf-8").splitlines(keepends=True)
    migrated_lines: list[str] = []
    normalized_timestamps = 0
    changed = False
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            migrated_lines.append(line)
            continue
        try:
            raw_event = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "旧版 Trace 迁移遇到无法解析的 JSON: "
                f"file={trace_file}, line={line_number}"
            ) from error
        if not isinstance(raw_event, dict):
            raise TypeError(
                "旧版 Trace 事件必须是 JSON object: "
                f"file={trace_file}, line={line_number}"
            )

        line_changed = False
        timestamp = raw_event.get("timestamp")
        if isinstance(timestamp, str):
            try:
                parsed_timestamp = datetime.fromisoformat(
                    timestamp
                )
            except ValueError:
                parsed_timestamp = None
            if parsed_timestamp is not None and parsed_timestamp.tzinfo is None:
                raw_event["timestamp"] = f"{timestamp}+00:00"
                normalized_timestamps += 1
                changed = True
                line_changed = True

        if line_changed:
            if line.endswith("\r\n"):
                line_ending = "\r\n"
            elif line.endswith("\n"):
                line_ending = "\n"
            else:
                line_ending = ""
            migrated_lines.append(
                json.dumps(raw_event, ensure_ascii=False, separators=(",", ":"))
                + line_ending
            )
        else:
            migrated_lines.append(line)

    if not changed:
        return 0

    backup_file.parent.mkdir(parents=True, exist_ok=True)
    if not backup_file.exists():
        shutil.copy2(trace_file, backup_file)
    temporary_file = trace_file.with_name(f".{trace_file.name}.migration-tmp")
    if temporary_file.exists():
        raise RuntimeError(
            "旧版 Trace 迁移临时文件已存在，拒绝覆盖: "
            f"file={temporary_file}"
        )
    try:
        temporary_file.write_text("".join(migrated_lines), encoding="utf-8")
        os.replace(temporary_file, trace_file)
    finally:
        temporary_file.unlink(missing_ok=True)
    return normalized_timestamps


def migrate_legacy_trace_timestamps(
    *,
    boxteam_root: Path,
    sessions_root: Path,
) -> None:
    """一次性修复旧版 Trace 的无时区 timestamp，并保留原始文件备份。"""
    migration_path = boxteam_root / "migrations" / "trace-timestamps-v1.json"
    if migration_path.exists():
        return

    # TODO: 兼容旧版本曾写入的 naive datetime；新代码必须始终写入带时区时间。
    backup_root = boxteam_root / "migrations" / "trace-timestamps-v1-backup"
    migrated_files: list[str] = []
    normalized_timestamps = 0
    if sessions_root.exists():
        trace_files = [
            trace_file
            for trace_file in sessions_root.rglob("*.jsonl")
            if trace_file.name in {"events.jsonl", "messages.jsonl"}
        ]
        for trace_file in sorted(trace_files):
            relative_path = trace_file.relative_to(sessions_root)
            backup_file = backup_root / relative_path
            count = _normalize_legacy_trace_file(
                trace_file=trace_file,
                backup_file=backup_file,
            )
            if count:
                migrated_files.append(str(relative_path))
                normalized_timestamps += count

    migration_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = migration_path.with_name(f".{migration_path.name}.tmp")
    temporary_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "normalized_timestamps": normalized_timestamps,
                "migrated_files": migrated_files,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, migration_path)


def migrate_workspace_storage_layout(
    *,
    boxteam_root: Path,
    sessions_root: Path,
) -> None:
    """把当前工作区旧的分散会话数据迁入其稳定 ID 对应的物理会话节点。"""
    if not sessions_root.exists():
        return

    for session_file in sessions_root.rglob("session.json"):
        session_dir = session_file.parent
        if session_dir.is_symlink():
            continue
        raw_session = json.loads(session_file.read_text(encoding="utf-8"))
        if not isinstance(raw_session, dict):
            raise TypeError(f"会话 manifest 必须是 JSON object: {session_file}")
        session_id = raw_session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError(f"会话 manifest 缺少 session_id: {session_file}")
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
    _move_legacy_path(
        legacy_config_root / "boxteam.jsonc",
        config_root / "boxteam.jsonc",
    )
    _move_legacy_path(
        legacy_config_root / "config.schema.jsonc",
        boxteam_home / "state" / "migrated" / "legacy_config.schema.jsonc",
    )
    _move_legacy_path(
        default_workspace_root / ".boxteam" / "gateway",
        boxteam_home / "state" / "gateway",
    )
    legacy_ui_settings = legacy_config_root / "web_ui_settings.json"
    if legacy_ui_settings.exists():
        current_ui_settings = (
            boxteam_home / "state" / "gateway" / "web_ui_settings.json"
        )
        ui_target = (
            boxteam_home / "state" / "migrated" / "legacy_web_ui_settings.json"
            if current_ui_settings.exists()
            else current_ui_settings
        )
        _move_legacy_path(legacy_ui_settings, ui_target)
