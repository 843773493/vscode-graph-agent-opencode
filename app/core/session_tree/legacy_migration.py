from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from app.core.session_tree.nodes import write_folder_manifest
from app.core.session_tree.support import (
    FOLDER_MANIFEST_NAME,
    SESSION_MANIFEST_NAME,
    _atomic_write_json,
    _parse_optional_datetime,
    _process_identity,
    _read_json_object,
    physical_segment,
)


class SessionLegacyLayoutMigrationSupport:
    """负责旧平面会话布局向物理目录树的一次性迁移。"""

    # TODO: 完成旧布局发布迁移窗口后，删除对 session-folders.json 的读取入口。
    def _migrate_legacy_layout(self) -> None:
        boxteam_root = self.sessions_root.parent
        migration_dir = boxteam_root / "migrations"
        migration_path = migration_dir / "session-physical-layout-v1.json"
        if migration_path.exists():
            record = _read_json_object(migration_path)
            if record.get("status") == "completed":
                return

        migration_dir.mkdir(parents=True, exist_ok=True)
        lock_path = migration_dir / "session-physical-layout-v1.lock"
        lock_descriptor = self._acquire_migration_lock(lock_path)
        try:
            self._write_migration_lock_owner(lock_descriptor)
            self._migrate_legacy_layout_locked(migration_path)
        finally:
            self._release_migration_lock(lock_descriptor)

    @staticmethod
    def _acquire_migration_lock(lock_path: Path) -> int:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                # TODO: Windows CI 覆盖 msvcrt 单字节非阻塞锁的跨进程行为。
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b" ")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            os.close(descriptor)
            raise RuntimeError(
                f"会话物理目录迁移已被另一个进程占用: {lock_path}"
            ) from error
        return descriptor

    @staticmethod
    def _write_migration_lock_owner(descriptor: int) -> None:
        encoded = json.dumps(
            {
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
                "process_identity": _process_identity(os.getpid()),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, encoded)
        os.fsync(descriptor)

    @staticmethod
    def _release_migration_lock(descriptor: int) -> None:
        try:
            if os.name == "nt":
                # TODO: Windows CI 覆盖 msvcrt 单字节锁释放行为。
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _migrate_legacy_layout_locked(self, migration_path: Path) -> None:
        if migration_path.exists():
            record = _read_json_object(migration_path)
            if record.get("status") == "completed":
                return
        boxteam_root = self.sessions_root.parent
        migration_dir = migration_path.parent

        legacy_navigation_path = boxteam_root / "navigation" / "session-folders.json"
        legacy_navigation = (
            _read_json_object(legacy_navigation_path)
            if legacy_navigation_path.exists()
            else {"folders": [], "session_parents": {}}
        )
        raw_folders = legacy_navigation.get("folders", [])
        raw_session_parents = legacy_navigation.get("session_parents", {})
        if not isinstance(raw_folders, list) or not isinstance(raw_session_parents, dict):
            raise RuntimeError(f"旧会话导航格式无效: {legacy_navigation_path}")
        self._validate_legacy_navigation(
            raw_folders=raw_folders,
            raw_session_parents=raw_session_parents,
            session_paths=self._discover_session_paths(),
        )

        existing_record = (
            _read_json_object(migration_path) if migration_path.exists() else None
        )
        raw_existing_operations = (
            existing_record.get("operations", []) if existing_record is not None else []
        )
        if not isinstance(raw_existing_operations, list):
            raise RuntimeError(f"会话物理目录迁移 operations 无效: {migration_path}")
        operations = [
            {str(key): str(value) for key, value in item.items()}
            for item in raw_existing_operations
            if isinstance(item, dict)
        ]
        started_at = (
            existing_record.get("started_at")
            if existing_record is not None
            else datetime.now(UTC).isoformat()
        )
        if not isinstance(started_at, str):
            started_at = datetime.now(UTC).isoformat()
        _atomic_write_json(
            migration_path,
            {
                "schema_version": 1,
                "status": "executing",
                "started_at": started_at,
                "operations": operations,
            },
        )
        try:
            self._migrate_nodes(
                raw_folders=raw_folders,
                raw_session_parents=raw_session_parents,
                operations=operations,
                migration_path=migration_path,
            )
            self._scan_physical_tree_locked()
            archived_navigation: str | None = None
            if legacy_navigation_path.exists():
                archive_path = migration_dir / "session-folders-v1.json"
                if archive_path.exists():
                    raise FileExistsError(f"旧会话导航归档目标已存在: {archive_path}")
                os.replace(legacy_navigation_path, archive_path)
                archived_navigation = str(archive_path)
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 1,
                    "status": "completed",
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "operations": operations,
                    "archived_navigation": archived_navigation,
                },
            )
        except Exception as error:
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 1,
                    "status": "failed",
                    "started_at": started_at,
                    "failed_at": datetime.now(UTC).isoformat(),
                    "operations": operations,
                    "error": f"{type(error).__name__}: {error}",
                },
            )
            raise

    @staticmethod
    def _validate_legacy_navigation(
        *,
        raw_folders: list[object],
        raw_session_parents: dict[object, object],
        session_paths: dict[str, Path],
    ) -> None:
        """在任何物理目录变更前验证旧导航图可以转换为文件夹树。"""
        folders: dict[str, str | None] = {}
        session_parents: dict[str, str] = {}
        for raw_folder in raw_folders:
            if not isinstance(raw_folder, dict):
                raise RuntimeError("旧会话文件夹记录必须是 object")
            folder_id = raw_folder.get("folder_id")
            if not isinstance(folder_id, str) or not folder_id:
                raise RuntimeError("旧会话文件夹记录缺少 folder_id")
            if folder_id in folders:
                raise RuntimeError(f"旧会话文件夹 ID 重复: {folder_id}")
            if folder_id in session_paths:
                raise RuntimeError(f"旧文件夹 ID 与会话 ID 冲突: {folder_id}")
            name = raw_folder.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RuntimeError(f"旧会话文件夹名称无效: {folder_id}")
            raw_parent = raw_folder.get("parent_folder_id")
            if raw_parent is not None and (
                not isinstance(raw_parent, str) or not raw_parent
            ):
                raise RuntimeError(f"旧会话文件夹父节点无效: {folder_id}")
            folders[folder_id] = raw_parent
            raw_session_ids = raw_folder.get("session_ids", [])
            if not isinstance(raw_session_ids, list):
                raise RuntimeError(f"旧会话文件夹 session_ids 无效: {folder_id}")
            for session_id in raw_session_ids:
                if not isinstance(session_id, str) or not session_id:
                    raise RuntimeError(f"旧会话 ID 无效: folder_id={folder_id}")
                if session_id in session_parents:
                    raise RuntimeError(f"旧导航中会话存在多个父节点: {session_id}")
                session_parents[session_id] = folder_id

        for session_id, parent_id in raw_session_parents.items():
            if not isinstance(session_id, str) or not isinstance(parent_id, str):
                raise RuntimeError("旧 session_parents 必须是字符串映射")
            if session_id in session_parents:
                raise RuntimeError(f"旧导航中会话存在多个父节点: {session_id}")
            session_parents[session_id] = parent_id

        for folder_id, parent_id in folders.items():
            if parent_id is not None and parent_id not in folders:
                relation_kind = "会话" if parent_id in session_paths else "不存在节点"
                raise RuntimeError(
                    "旧会话文件夹只能挂在文件夹下: "
                    f"folder_id={folder_id}, parent_id={parent_id}, parent_kind={relation_kind}"
                )
        for session_id, parent_id in session_parents.items():
            if session_id not in session_paths:
                raise RuntimeError(f"旧会话导航引用不存在的会话: {session_id}")
            if parent_id not in folders:
                raise RuntimeError(
                    "旧会话只能挂在文件夹下: "
                    f"session_id={session_id}, parent_id={parent_id}"
                )

        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(folder_id: str) -> None:
            if folder_id in visited:
                return
            if folder_id in visiting:
                raise RuntimeError(f"旧会话文件夹关系包含循环: {folder_id}")
            visiting.add(folder_id)
            parent_id = folders[folder_id]
            if parent_id is not None:
                visit(parent_id)
            visiting.remove(folder_id)
            visited.add(folder_id)

        for folder_id in folders:
            visit(folder_id)

    def _migrate_nodes(
        self,
        *,
        raw_folders: list[object],
        raw_session_parents: dict[object, object],
        operations: list[dict[str, str]],
        migration_path: Path,
    ) -> None:
        folders: dict[str, dict[str, object]] = {}
        parent_by_node: dict[str, str] = {}
        for raw_folder in raw_folders:
            if not isinstance(raw_folder, dict):
                raise RuntimeError("旧会话文件夹记录必须是 object")
            folder_id = raw_folder.get("folder_id")
            if not isinstance(folder_id, str) or not folder_id:
                raise RuntimeError("旧会话文件夹记录缺少 folder_id")
            if folder_id in folders:
                raise RuntimeError(f"旧会话文件夹 ID 重复: {folder_id}")
            folders[folder_id] = raw_folder
            parent = raw_folder.get("parent_folder_id")
            if isinstance(parent, str):
                parent_by_node[folder_id] = parent
            session_ids = raw_folder.get("session_ids", [])
            if not isinstance(session_ids, list):
                raise RuntimeError(f"旧会话文件夹 session_ids 无效: {folder_id}")
            for session_id in session_ids:
                if not isinstance(session_id, str):
                    raise RuntimeError(f"旧会话 ID 无效: folder_id={folder_id}")
                if session_id in parent_by_node:
                    raise RuntimeError(f"旧导航中会话存在多个父节点: {session_id}")
                parent_by_node[session_id] = folder_id
        for session_id, parent_id in raw_session_parents.items():
            if not isinstance(session_id, str) or not isinstance(parent_id, str):
                raise RuntimeError("旧 session_parents 必须是字符串映射")
            if session_id in parent_by_node:
                raise RuntimeError(f"旧导航中会话存在多个父节点: {session_id}")
            parent_by_node[session_id] = parent_id

        session_paths = self._discover_session_paths()
        folder_paths: dict[str, Path] = {}
        visiting: set[str] = set()

        def place(node_id: str) -> Path:
            if node_id in folder_paths:
                return folder_paths[node_id]
            if node_id in session_paths and not self._is_legacy_flat_path(session_paths[node_id]):
                return session_paths[node_id]
            if node_id in visiting:
                raise RuntimeError(f"旧会话导航包含循环: {node_id}")
            visiting.add(node_id)
            parent_id = parent_by_node.get(node_id)
            parent_path = self.sessions_root if parent_id is None else place(parent_id)

            if node_id in folders:
                raw_folder = folders[node_id]
                name = raw_folder.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise RuntimeError(f"旧会话文件夹名称无效: {node_id}")
                target = parent_path / physical_segment(name, node_id)
                if target.exists():
                    manifest_path = target / FOLDER_MANIFEST_NAME
                    if not manifest_path.exists():
                        raise FileExistsError(f"迁移文件夹目标已存在且无 manifest: {target}")
                    existing_id = _read_json_object(manifest_path).get("folder_id")
                    if existing_id != node_id:
                        raise RuntimeError(f"迁移文件夹目标 ID 冲突: {target}")
                else:
                    target.mkdir()
                    created_at = _parse_optional_datetime(raw_folder.get("created_at"))
                    write_folder_manifest(
                        target,
                        folder_id=node_id,
                        created_at=created_at or datetime.now(UTC),
                    )
                    self._append_migration_operation(
                        operations,
                        migration_path,
                        operation="create_folder",
                        node_id=node_id,
                        source="",
                        target=str(target),
                    )
                folder_paths[node_id] = target
                visiting.remove(node_id)
                return target

            source = session_paths.get(node_id)
            if source is None:
                raise RuntimeError(f"旧会话导航引用不存在的会话: {node_id}")
            raw_session = _read_json_object(source / SESSION_MANIFEST_NAME)
            title = raw_session.get("title")
            if not isinstance(title, str):
                raise RuntimeError(f"旧会话标题无效: {source}")
            target = parent_path / physical_segment(title, node_id)
            if source != target:
                if target.exists():
                    raise FileExistsError(f"迁移会话目标已存在: {target}")
                os.replace(source, target)
                session_paths[node_id] = target
                self._append_migration_operation(
                    operations,
                    migration_path,
                    operation="move_session",
                    node_id=node_id,
                    source=str(source),
                    target=str(target),
                )
            visiting.remove(node_id)
            return target

        for folder_id in sorted(folders):
            place(folder_id)
        for session_id in sorted(session_paths):
            place(session_id)

    def _discover_session_paths(self) -> dict[str, Path]:
        paths_by_id: dict[str, list[Path]] = {}
        for manifest_path in self.sessions_root.rglob(SESSION_MANIFEST_NAME):
            if manifest_path.is_symlink():
                continue
            raw = _read_json_object(manifest_path)
            session_id = raw.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError(f"会话 manifest 缺少 session_id: {manifest_path}")
            paths_by_id.setdefault(session_id, []).append(manifest_path.parent)
        duplicates = {
            node_id: paths for node_id, paths in paths_by_id.items() if len(paths) > 1
        }
        if duplicates:
            details = "; ".join(
                f"{node_id}={','.join(str(path) for path in paths)}"
                for node_id, paths in sorted(duplicates.items())
            )
            raise RuntimeError(f"迁移前发现重复会话 ID: {details}")
        return {node_id: paths[0] for node_id, paths in paths_by_id.items()}

    def _is_legacy_flat_path(self, path: Path) -> bool:
        return path.parent == self.sessions_root and path.name == _read_json_object(
            path / SESSION_MANIFEST_NAME
        ).get("session_id")

    @staticmethod
    def _append_migration_operation(
        operations: list[dict[str, str]],
        migration_path: Path,
        *,
        operation: str,
        node_id: str,
        source: str,
        target: str,
    ) -> None:
        operations.append(
            {
                "operation": operation,
                "node_id": node_id,
                "source": source,
                "target": target,
            }
        )
        record = _read_json_object(migration_path)
        record["operations"] = operations
        _atomic_write_json(migration_path, record)
