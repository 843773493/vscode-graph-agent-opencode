from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from app.core.legacy_inline_attachment_migration import (
    materialize_legacy_inline_attachments,
)
from app.core.session_tree.legacy_migration import (
    SessionLegacyLayoutMigrationSupport,
)
from app.core.session_tree.support import (
    SESSION_CHILDREN_DIR_NAME,
    SESSION_MANIFEST_NAME,
    _atomic_write_json,
    _atomic_write_json_value,
    _atomic_write_text,
    _read_json_object,
    _rewrite_checkpoint_blob,
    _rewrite_legacy_locator_value,
    physical_segment,
)


class SessionMetadataMigrationSupport(SessionLegacyLayoutMigrationSupport):
    """负责会话父子关系、定位符和旧附件的一次性迁移。"""

    def _migrate_physical_session_parents(self) -> None:
        """把既有 manifest 父子关系一次性转换为真实会话子树。"""
        migration_dir = self.sessions_root.parent / "migrations"
        migration_path = migration_dir / "session-physical-parents-v2.json"
        if migration_path.exists():
            record = _read_json_object(migration_path)
            if record.get("status") == "completed":
                return

        migration_dir.mkdir(parents=True, exist_ok=True)
        lock_path = migration_dir / "session-physical-parents-v2.lock"
        lock_descriptor = self._acquire_migration_lock(lock_path)
        try:
            self._write_migration_lock_owner(lock_descriptor)
            operations: list[dict[str, str]] = []
            started_at = datetime.now(UTC).isoformat()
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 2,
                    "status": "executing",
                    "started_at": started_at,
                    "operations": operations,
                },
            )
            try:
                self._migrate_physical_session_parents_locked(
                    operations=operations,
                    migration_path=migration_path,
                )
            except Exception as error:
                _atomic_write_json(
                    migration_path,
                    {
                        "schema_version": 2,
                        "status": "failed",
                        "started_at": started_at,
                        "failed_at": datetime.now(UTC).isoformat(),
                        "operations": operations,
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
                raise
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 2,
                    "status": "completed",
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "operations": operations,
                },
            )
        finally:
            self._release_migration_lock(lock_descriptor)

    def _migrate_physical_session_parents_locked(
        self,
        *,
        operations: list[dict[str, str]],
        migration_path: Path,
    ) -> None:
        initial_paths = self._discover_session_paths()
        parent_by_session: dict[str, str | None] = {}
        workspace_by_session: dict[str, str | None] = {}
        for session_id, path in initial_paths.items():
            raw = _read_json_object(path / SESSION_MANIFEST_NAME)
            parent_id = raw.get("parent_session_id")
            if parent_id is not None and not isinstance(parent_id, str):
                raise RuntimeError(
                    "会话 parent_session_id 必须是字符串或 null: "
                    f"session_id={session_id}, value={parent_id!r}"
                )
            parent_by_session[session_id] = parent_id
            if raw.get("kind") == "context_fork" and not isinstance(
                raw.get("context_source_session_id"),
                str,
            ):
                if parent_id is None:
                    raise RuntimeError(
                        "旧 context_fork 会话缺少可迁移的上下文来源: "
                        f"session_id={session_id}"
                    )
                raw["context_source_session_id"] = parent_id
                _atomic_write_json(path / SESSION_MANIFEST_NAME, raw)
                self._append_migration_operation(
                    operations,
                    migration_path,
                    operation="record_context_source",
                    node_id=session_id,
                    source=str(path / SESSION_MANIFEST_NAME),
                    target=str(path / SESSION_MANIFEST_NAME),
                )
            workspace_id = raw.get("workspace_id")
            workspace_by_session[session_id] = (
                workspace_id if isinstance(workspace_id, str) else None
            )

        for session_id, parent_id in parent_by_session.items():
            if parent_id is None:
                continue
            if parent_id not in initial_paths:
                raise RuntimeError(
                    "会话父节点不存在，无法迁移物理子树: "
                    f"session_id={session_id}, parent_session_id={parent_id}"
                )
            child_workspace = workspace_by_session[session_id]
            parent_workspace = workspace_by_session[parent_id]
            if (
                child_workspace is not None
                and parent_workspace is not None
                and child_workspace != parent_workspace
            ):
                raise RuntimeError(
                    "跨工作区父子会话不能迁移为物理子树: "
                    f"session_id={session_id}, child_workspace={child_workspace}, "
                    f"parent_workspace={parent_workspace}"
                )

        placed: set[str] = set()
        visiting: set[str] = set()

        def place(session_id: str) -> None:
            if session_id in placed:
                return
            if session_id in visiting:
                raise RuntimeError(f"会话父子关系包含循环: {session_id}")
            visiting.add(session_id)
            parent_id = parent_by_session[session_id]
            if parent_id is not None:
                place(parent_id)

            current_paths = self._discover_session_paths()
            source = current_paths[session_id]
            actual_parent_id = self._nearest_physical_session_id(
                source,
                current_paths,
            )
            if actual_parent_id != parent_id:
                if actual_parent_id is not None:
                    raise RuntimeError(
                        "既有物理父会话与 manifest 不一致，拒绝猜测迁移: "
                        f"session_id={session_id}, declared={parent_id}, "
                        f"physical={actual_parent_id}"
                    )
                if parent_id is not None:
                    parent_path = current_paths[parent_id]
                    children_path = parent_path / SESSION_CHILDREN_DIR_NAME
                    children_path.mkdir(exist_ok=True)
                    raw = _read_json_object(source / SESSION_MANIFEST_NAME)
                    title = raw.get("title")
                    if not isinstance(title, str):
                        raise RuntimeError(f"旧会话标题无效: {source}")
                    target = children_path / physical_segment(title, session_id)
                    if target.exists():
                        raise FileExistsError(f"迁移子会话目标已存在: {target}")
                    os.replace(source, target)
                    self._append_migration_operation(
                        operations,
                        migration_path,
                        operation="move_child_session",
                        node_id=session_id,
                        source=str(source),
                        target=str(target),
                    )
            visiting.remove(session_id)
            placed.add(session_id)

        for session_id in sorted(initial_paths):
            place(session_id)

    def _nearest_physical_session_id(
        self,
        session_path: Path,
        session_paths: dict[str, Path],
    ) -> str | None:
        ids_by_path = {
            path.resolve(): session_id for session_id, path in session_paths.items()
        }
        current = session_path.resolve().parent
        while current != self.sessions_root:
            session_id = ids_by_path.get(current)
            if session_id is not None:
                return session_id
            if not current.is_relative_to(self.sessions_root):
                raise RuntimeError(f"会话目录越出 sessions 根目录: {session_path}")
            current = current.parent
        return None

    def _migrate_legacy_session_locators(self) -> None:
        migration_dir = self.sessions_root.parent / "migrations"
        migration_path = migration_dir / "session-stable-locators-v1.json"
        if migration_path.exists():
            record = _read_json_object(migration_path)
            if record.get("status") == "completed":
                return
        migration_dir.mkdir(parents=True, exist_ok=True)
        lock_path = migration_dir / "session-stable-locators-v1.lock"
        lock_descriptor = self._acquire_migration_lock(lock_path)
        try:
            self._write_migration_lock_owner(lock_descriptor)
            rewritten_files: list[str] = []
            started_at = datetime.now(UTC).isoformat()
            try:
                for session_id, session_path in self._discover_session_paths().items():
                    rewritten_files.extend(
                        self._rewrite_session_locator_files(session_id, session_path)
                    )
            except Exception as error:
                _atomic_write_json(
                    migration_path,
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "started_at": started_at,
                        "failed_at": datetime.now(UTC).isoformat(),
                        "rewritten_files": rewritten_files,
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
                raise
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 1,
                    "status": "completed",
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "rewritten_files": rewritten_files,
                },
            )
        finally:
            self._release_migration_lock(lock_descriptor)

    def _rewrite_session_locator_files(
        self,
        session_id: str,
        session_path: Path,
        *,
        attachment_locators: dict[str, str] | None = None,
    ) -> list[str]:
        rewritten: list[str] = []
        for path in sorted(session_path.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative_path = path.relative_to(session_path)
            top_level = relative_path.parts[0]
            if top_level == SESSION_CHILDREN_DIR_NAME:
                continue
            if not (
                relative_path == Path("pending_requests.json")
                or top_level in {"message_history", "checkpoints"}
            ):
                continue
            changed = False
            if path.suffix == ".json":
                value = json.loads(path.read_text(encoding="utf-8"))
                value, changed = _rewrite_legacy_locator_value(
                    value,
                    session_id,
                    attachment_locators=attachment_locators,
                )
                if changed:
                    _atomic_write_json_value(path, value)
            elif path.suffix == ".jsonl":
                output_lines: list[str] = []
                for line_number, raw_line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(),
                    start=1,
                ):
                    if not raw_line.strip():
                        continue
                    try:
                        value = json.loads(raw_line)
                    except json.JSONDecodeError as error:
                        raise RuntimeError(
                            "迁移稳定会话定位符时遇到损坏 JSONL: "
                            f"path={path}, line={line_number}"
                        ) from error
                    value, line_changed = _rewrite_legacy_locator_value(
                        value,
                        session_id,
                        attachment_locators=attachment_locators,
                    )
                    changed = changed or line_changed
                    output_lines.append(
                        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    )
                if changed:
                    _atomic_write_text(path, "\n".join(output_lines) + "\n")
            elif (
                top_level == "checkpoints"
                and path.parent.name == "blobs"
                and path.suffix == ".bin"
            ):
                changed = _rewrite_checkpoint_blob(
                    path,
                    session_id,
                    attachment_locators=attachment_locators,
                )
            if changed:
                rewritten.append(str(path))
        return rewritten

    def _migrate_legacy_inline_attachments(self) -> None:
        migration_dir = self.sessions_root.parent / "migrations"
        migration_path = migration_dir / "session-inline-attachments-v1.json"
        if migration_path.exists():
            record = _read_json_object(migration_path)
            if record.get("status") == "completed":
                return
        migration_dir.mkdir(parents=True, exist_ok=True)
        lock_path = migration_dir / "session-inline-attachments-v1.lock"
        lock_descriptor = self._acquire_migration_lock(lock_path)
        try:
            self._write_migration_lock_owner(lock_descriptor)
            started_at = datetime.now(UTC).isoformat()
            rewritten_files: list[str] = []
            materialized: dict[str, int] = {}
            try:
                for session_id, session_path in self._discover_session_paths().items():
                    locators = materialize_legacy_inline_attachments(
                        session_id=session_id,
                        session_path=session_path,
                    )
                    materialized[session_id] = len(locators)
                    rewritten_files.extend(
                        self._rewrite_session_locator_files(
                            session_id,
                            session_path,
                            attachment_locators=locators,
                        )
                    )
            except Exception as error:
                _atomic_write_json(
                    migration_path,
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "started_at": started_at,
                        "failed_at": datetime.now(UTC).isoformat(),
                        "materialized": materialized,
                        "rewritten_files": rewritten_files,
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
                raise
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 1,
                    "status": "completed",
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "materialized": materialized,
                    "rewritten_files": rewritten_files,
                },
            )
        finally:
            self._release_migration_lock(lock_descriptor)
