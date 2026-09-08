"""rollout SQLite backup/restore owner。

备份属于可恢复的 storage artifact 生命周期，不与 JSONL/SQLite 完整性校验
或 checkpoint 事务实现混在同一个 owner 中。compaction 和启动恢复只复用
这里的文件复制原语，不自行实现第二套 SQLite backup 协议。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


class RolloutStorageBackupMixin:
    """提供显式 SQLite backup 与恢复能力。"""

    @staticmethod
    def _require_regular_file(path: Path, *, field: str) -> None:
        if path.is_symlink():
            raise RuntimeError(f"{field} 不能是符号链接: {path}")
        if path.exists() and not path.is_file():
            raise RuntimeError(f"{field} 必须是普通文件: {path}")

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _copy_file_fsync(source: Path, target: Path) -> None:
        with source.open("rb") as source_stream, target.open("wb") as target_stream:
            shutil.copyfileobj(source_stream, target_stream)
            target_stream.flush()
            os.fsync(target_stream.fileno())

    def backup_index(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        destination: str | Path | None = None,
    ) -> Path:
        """使用 SQLite backup API 创建可恢复的 index 副本。"""
        thread_id = strict_text(thread_id, field="backup.thread_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("backup.checkpoint_ns 必须是字符串")
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._validate_schema_state(connection)
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(
                        f"SQLite backup integrity_check 失败: {integrity}"
                    )
            return self._backup_index_unlocked(
                thread_id, checkpoint_ns, destination=destination
            )

    def _backup_index_unlocked(
        self,
        thread_id: str,
        checkpoint_ns: str,
        *,
        destination: str | Path | None,
    ) -> Path:
        """在调用方已持有 rollout 写锁时复制 SQLite。"""
        source_path = self.index_path(thread_id, checkpoint_ns)
        self._require_regular_file(source_path, field="SQLite backup source")
        target_path = (
            Path(destination).absolute()
            if destination
            else source_path.with_name("index.sqlite.backup")
        )
        if target_path == source_path:
            raise ValueError("SQLite backup 目标不能覆盖当前 index")
        self._require_regular_file(target_path, field="SQLite backup target")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
        try:
            source = sqlite3.connect(source_path)
            target = sqlite3.connect(temporary_path)
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
    ) -> RolloutReadSnapshot:
        """恢复显式指定的 SQLite backup。"""
        thread_id = strict_text(thread_id, field="restore.thread_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("restore.checkpoint_ns 必须是字符串")
        source_path = Path(backup_path).absolute()
        self._require_regular_file(source_path, field="SQLite restore source")
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        with self._lock(thread_id, checkpoint_ns):
            self._restore_index_backup_unlocked(thread_id, checkpoint_ns, source_path)
        return self.validate_index(thread_id, checkpoint_ns)

    def _restore_index_backup_unlocked(
        self,
        thread_id: str,
        checkpoint_ns: str,
        source_path: Path,
    ) -> None:
        """持有 rollout 写锁，通过 SQLite 事务写回原 inode，保留 reader 快照。"""
        target_path = self.index_path(thread_id, checkpoint_ns)
        source_path = source_path.absolute()
        for role, path in (("source", source_path), ("target", target_path)):
            self._require_regular_file(path, field=f"SQLite restore {role}")
            for component in path.parents:
                if component.is_symlink():
                    raise RuntimeError(
                        f"SQLite restore {role} 父目录不能是符号链接: {component}"
                    )
            # SQLite 自己管理 sidecar；不能跟随符号链接，也不能清除它们。
            for suffix in ("-wal", "-shm", "-journal"):
                self._require_regular_file(
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
                    f"{source_path.as_uri()}?mode=ro&cache=private", uri=True, timeout=0
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
                            f"{target_path.as_uri()}?mode=rw&cache=private",
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


__all__ = ["RolloutStorageBackupMixin"]
