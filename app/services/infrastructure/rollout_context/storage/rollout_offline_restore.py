"""rollout index 备份与离线恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path
from uuid import uuid4

from app.core.atomic_fs import fsync_directory, fsync_file
from app.core.sqlite_state import sqlite_file_uri
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text

__all__ = ["RolloutOfflineRestoreMixin"]


class RolloutOfflineRestoreMixin:
    def _validate_offline_restore_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
    ) -> None:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError(
                "SQLite offline restore source integrity_check 失败: "
                f"thread_id={thread_id}: {integrity}"
            )
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(
                "SQLite offline restore source foreign_key_check 失败: "
                f"thread_id={thread_id}: {foreign_keys}"
            )
        self._validate_schema_state(connection)
        self._host._require_v2_runtime(connection)
        owner = connection.execute(
            "SELECT session_id, thread_id FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if owner != (session_id, thread_id):
            raise RuntimeError(
                "SQLite offline restore source session identity 不匹配: "
                f"expected={(session_id, thread_id)}, actual={owner}"
            )
        self._validate_v2_commit_offsets(
            connection,
            self._host.jsonl_path(session_id, checkpoint_ns, thread_id=thread_id),
            validate_jsonl_items=True,
        )

    def _prepare_offline_restore_candidate(
        self,
        source_path: Path,
        candidate_path: Path,
        *,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
    ) -> str:
        source_hash = _file_hash(source_path)
        with (
            closing(
                sqlite3.connect(
                    f"{sqlite_file_uri(source_path)}?mode=ro&cache=private",
                    uri=True,
                    timeout=0,
                )
            ) as source,
            closing(sqlite3.connect(sqlite_file_uri(candidate_path), uri=True)) as candidate,
        ):
            source.execute("BEGIN")
            if source.execute("PRAGMA page_count").fetchone()[0] == 0:
                raise RuntimeError(
                    f"SQLite offline restore source 是空数据库: {source_path}"
                )
            source.backup(candidate)
            candidate.commit()
        if _file_hash(source_path) != source_hash:
            raise RuntimeError(
                f"SQLite offline restore source 在验证期间发生变化: {source_path}"
            )
        os.chmod(candidate_path, 0o600)
        fsync_file(candidate_path)
        with closing(
            sqlite3.connect(
                f"{sqlite_file_uri(candidate_path)}?mode=ro&cache=private",
                uri=True,
                timeout=0,
            )
        ) as candidate:
            self._validate_offline_restore_candidate(
                candidate,
                session_id=session_id,
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
            )
        return source_hash

    def _require_standalone_offline_restore_source(self, source_path: Path) -> None:
        for component in source_path.parents:
            if component.is_symlink():
                raise RuntimeError(
                    f"SQLite offline restore source 父目录不能是符号链接: {component}"
                )
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{source_path}{suffix}")
            _require_regular_file(
                sidecar, field=f"SQLite offline restore source{suffix}"
            )
            if sidecar.exists():
                raise RuntimeError(
                    "SQLite offline restore source 必须是独立 backup artifact，"
                    f"不能依赖 sidecar: {sidecar}"
                )

    def backup_index(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        session_id: str,
        destination: str | Path | None = None,
    ) -> Path:
        """使用 SQLite backup API 创建可恢复的 index 副本。"""
        thread_id = strict_text(thread_id, field="backup.thread_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("backup.checkpoint_ns 必须是字符串")
        with self._host._lock(thread_id, checkpoint_ns, session_id=session_id):
            self.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            with self._host._connect(
                thread_id, checkpoint_ns, session_id=session_id
            ) as connection:
                self._validate_schema_state(connection)
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(
                        f"SQLite backup integrity_check 失败: {integrity}"
                    )
            return self._backup_index_unlocked(
                thread_id,
                checkpoint_ns,
                session_id=session_id,
                destination=destination,
            )

    def _backup_index_unlocked(
        self,
        thread_id: str,
        checkpoint_ns: str,
        *,
        session_id: str,
        destination: str | Path | None,
    ) -> Path:
        """在调用方已持有 rollout 写锁时复制 SQLite。"""
        source_path = self._host.index_path(
            session_id, checkpoint_ns, thread_id=thread_id
        )
        _require_regular_file(source_path, field="SQLite backup source")
        target_path = (
            Path(destination).absolute()
            if destination
            else source_path.with_name("index.sqlite.backup")
        )
        if target_path == source_path:
            raise ValueError("SQLite backup 目标不能覆盖当前 index")
        _require_regular_file(target_path, field="SQLite backup target")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
        try:
            source = sqlite3.connect(sqlite_file_uri(source_path), uri=True)
            target = sqlite3.connect(sqlite_file_uri(temporary_path), uri=True)
            try:
                source.backup(target)
                target.commit()
                integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(
                        f"SQLite backup integrity_check 失败: {integrity}"
                    )
            finally:
                target.close()
                source.close()
            os.replace(temporary_path, target_path)
        finally:
            if temporary_path.exists() or temporary_path.is_symlink():
                temporary_path.unlink()
        return target_path

    def restore_index_backup(
        self,
        thread_id: str,
        backup_path: str | Path,
        checkpoint_ns: str = "",
        *,
        session_id: str,
    ) -> RolloutReadSnapshot:
        """恢复显式指定的 SQLite backup。"""
        thread_id = strict_text(thread_id, field="restore.thread_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("restore.checkpoint_ns 必须是字符串")
        source_path = Path(backup_path).absolute()
        _require_regular_file(source_path, field="SQLite restore source")
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        with self._host._lock(thread_id, checkpoint_ns, session_id=session_id):
            self._restore_index_backup_unlocked(
                thread_id, checkpoint_ns, source_path, session_id=session_id
            )
        return self.validate_index(session_id, thread_id, checkpoint_ns)

    def restore_index_backup_offline(
        self,
        thread_id: str,
        backup_path: str | Path,
        checkpoint_ns: str = "",
        *,
        session_id: str,
    ) -> RolloutReadSnapshot:
        """显式离线恢复完全损坏的 index，并保留原文件供审计。

        调用方必须先停止该 session 的所有非 RolloutStorage SQLite reader。
        正常启动与读取路径绝不自动调用本方法。
        """
        thread_id = strict_text(thread_id, field="offline_restore.thread_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("offline_restore.checkpoint_ns 必须是字符串")
        source_path = Path(backup_path).absolute()
        _require_regular_file(source_path, field="SQLite offline restore source")
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        self._require_standalone_offline_restore_source(source_path)
        target_path = self._host.index_path(
            session_id, checkpoint_ns, thread_id=thread_id
        )
        target_root = target_path.parent
        candidate_path = target_root / f".index.sqlite.{uuid4().hex}.offline-restore"
        operation_id = uuid4().hex
        quarantine_root = target_root / "recovery-quarantine"
        quarantine_path = quarantine_root / operation_id

        with self._host._lock(thread_id, checkpoint_ns, session_id=session_id):
            _require_regular_file(
                target_path, field="SQLite offline restore target"
            )
            if not target_path.is_file():
                raise FileNotFoundError(target_path)
            for component in target_path.parents:
                if component.is_symlink():
                    raise RuntimeError(
                        "SQLite offline restore target 父目录不能是符号链接: "
                        f"{component}"
                    )
            if source_path.samefile(target_path):
                raise ValueError(
                    "SQLite offline restore source 与 target 不能是同一文件: "
                    f"{source_path} -> {target_path}"
                )
            _require_offline_restore_target(target_path)
            moved: list[tuple[Path, Path]] = []
            installed = False
            try:
                source_hash = self._prepare_offline_restore_candidate(
                    source_path,
                    candidate_path,
                    session_id=session_id,
                    thread_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                )
                _require_plain_directory(
                    quarantine_root,
                    field="SQLite offline restore quarantine root",
                )
                quarantine_root.mkdir(mode=0o700, exist_ok=True)
                quarantine_path.mkdir(mode=0o700)
                for original in (
                    target_path,
                    Path(f"{target_path}-wal"),
                    Path(f"{target_path}-shm"),
                    Path(f"{target_path}-journal"),
                ):
                    _require_regular_file(
                        original, field="SQLite offline restore quarantined artifact"
                    )
                    if original.exists():
                        quarantined = quarantine_path / original.name
                        os.replace(original, quarantined)
                        moved.append((original, quarantined))
                fsync_directory(quarantine_path)
                os.replace(candidate_path, target_path)
                installed = True
                fsync_directory(target_root)
                with closing(
                    sqlite3.connect(
                        f"{sqlite_file_uri(target_path)}?mode=ro&cache=private",
                        uri=True,
                        timeout=0,
                    )
                ) as restored:
                    self._validate_offline_restore_candidate(
                        restored,
                        session_id=session_id,
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                    )
                manifest = {
                    "schema": "rollout-index-offline-restore:v1",
                    "operation_id": operation_id,
                    "session_id": session_id,
                    "checkpoint_ns": checkpoint_ns,
                    "created_at": datetime.now(UTC).isoformat(),
                    "source_sha256": source_hash,
                    "installed_sha256": _file_hash(target_path),
                    "quarantined": [
                        {
                            "name": quarantined.name,
                            "sha256": _file_hash(quarantined),
                        }
                        for _, quarantined in moved
                    ],
                }
                manifest_path = quarantine_path / "restore-manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with manifest_path.open("rb") as stream:
                    os.fsync(stream.fileno())
                fsync_directory(quarantine_path)
                fsync_directory(quarantine_root)
            except BaseException:
                if installed and target_path.exists():
                    failed_target = quarantine_path / "failed-installed-index.sqlite"
                    os.replace(target_path, failed_target)
                for original, quarantined in reversed(moved):
                    if quarantined.exists():
                        os.replace(quarantined, original)
                if quarantine_path.is_dir() and not any(quarantine_path.iterdir()):
                    quarantine_path.rmdir()
                fsync_directory(target_root)
                raise
            finally:
                if candidate_path.exists() or candidate_path.is_symlink():
                    candidate_path.unlink()
        return self.validate_index(session_id, thread_id, checkpoint_ns)

    def _restore_index_backup_unlocked(
        self,
        thread_id: str,
        checkpoint_ns: str,
        source_path: Path,
        *,
        session_id: str,
    ) -> None:
        """持有 rollout 写锁，通过 SQLite 事务写回原 inode，保留 reader 快照。"""
        target_path = self._host.index_path(
            session_id, checkpoint_ns, thread_id=thread_id
        )
        source_path = source_path.absolute()
        for role, path in (("source", source_path), ("target", target_path)):
            _require_regular_file(path, field=f"SQLite restore {role}")
            for component in path.parents:
                if component.is_symlink():
                    raise RuntimeError(
                        f"SQLite restore {role} 父目录不能是符号链接: {component}"
                    )
            # SQLite 自己管理 sidecar；不能跟随符号链接，也不能清除它们。
            for suffix in ("-wal", "-shm", "-journal"):
                _require_regular_file(
                    Path(f"{path}{suffix}"), field=f"SQLite restore {role}{suffix}"
                )
        if source_path.samefile(target_path):
            raise ValueError(
                f"SQLite restore source 与 target 不能是同一文件: {source_path} -> {target_path}"
            )

        def require_backup_progress(status: int, remaining: int, total: int) -> None:
            # Python backup 默认无限重试 BUSY/LOCKED；明确失败，不能挂住恢复。
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise sqlite3.OperationalError(
                    f"SQLite restore database is locked: status={status}, "
                    f"remaining={remaining}, total={total}"
                )

        try:
            with closing(
                sqlite3.connect(
                    f"{sqlite_file_uri(source_path)}?mode=ro&cache=private", uri=True, timeout=0
                )
            ) as source:
                # 校验和复制固定在同一个只读快照，源备份不执行任何写操作。
                source.execute("BEGIN")
                if source.execute("PRAGMA page_count").fetchone()[0] == 0:
                    raise RuntimeError(
                        f"SQLite restore source 是空数据库: {source_path}"
                    )
                with closing(sqlite3.connect(":memory:")) as validated:
                    # SQLite 的只读 schema 会省略 CHECK 表达式；必须在私有
                    # 可写副本上校验，不能等写坏 target 后才发现源约束损坏。
                    page_size = source.execute("PRAGMA page_size").fetchone()[0]
                    validated.execute(f"PRAGMA page_size={page_size}")
                    source.backup(validated, progress=require_backup_progress, sleep=0)
                    integrity = validated.execute("PRAGMA integrity_check").fetchall()
                    if integrity != [("ok",)]:
                        raise RuntimeError(
                            f"SQLite restore source integrity_check 失败: {source_path}: {integrity}"
                        )
                    foreign_keys = validated.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                    if foreign_keys:
                        raise RuntimeError(
                            f"SQLite restore source foreign_key_check 失败: {source_path}: {foreign_keys}"
                        )
                    with closing(
                        sqlite3.connect(
                            f"{sqlite_file_uri(target_path)}?mode=rw&cache=private",
                            uri=True,
                            timeout=0,
                        )
                    ) as target:
                        # 不初始化、不替换、不清 WAL；损坏或只读 target 由 SQLite 拒绝。
                        validated.backup(
                            target, progress=require_backup_progress, sleep=0
                        )
                        integrity = target.execute("PRAGMA integrity_check").fetchall()
                        if integrity != [("ok",)]:
                            raise RuntimeError(
                                f"SQLite restore target integrity_check 失败: {target_path}: {integrity}"
                            )
        except sqlite3.Error as error:
            error.add_note(
                f"SQLite restore 失败: source={source_path}, target={target_path}; "
                "未替换数据库文件或手动清除 WAL/SHM"
            )
            raise


def _require_regular_file(path: Path, *, field: str) -> None:
    if path.is_symlink():
        raise RuntimeError(f"{field} 不能是符号链接: {path}")
    if path.exists() and not path.is_file():
        raise RuntimeError(f"{field} 必须是普通文件: {path}")

def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def _require_plain_directory(path: Path, *, field: str) -> None:
    if path.is_symlink():
        raise RuntimeError(f"{field} 不能是符号链接: {path}")
    if path.exists() and not path.is_dir():
        raise RuntimeError(f"{field} 必须是普通目录: {path}")

def _require_offline_restore_target(target_path: Path) -> None:
    try:
        with closing(
            sqlite3.connect(
                f"{sqlite_file_uri(target_path)}?mode=ro&cache=private",
                uri=True,
                timeout=0,
            )
        ) as target:
            target.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        return
    raise RuntimeError(
        "SQLite offline restore 只允许恢复无法打开的损坏 target；"
        "可打开的 target 请使用 restore_index_backup"
    )
