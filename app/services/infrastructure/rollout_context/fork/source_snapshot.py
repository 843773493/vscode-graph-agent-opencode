"""在 source 生命周期读 guard 内冻结 rollout 文件快照。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from app.core.atomic_fs import fsync_directory
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.services.infrastructure.attachment_blob_catalog.records import (
    ForkAttachmentSnapshot,
)
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugSourceCopySnapshot,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.fork.full_copy.sqlite_snapshot import (
    SQLITE_COPY_FILES,
    copy_live_sqlite_snapshot,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutFileLock,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore

_PRIVATE_SOURCE_FILES = frozenset(
    {
        "index.sqlite-shm",
    }
)


@dataclass(frozen=True, slots=True)
class ForkSnapshotFile:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ForkRolloutSourceSnapshot:
    source_session_id: str
    source_thread_id: str
    # 本次 fork 的私有捕获副本节点，不指向 live 源会话；source scope 仅保留来源历史 owner 事实，不能据此反推物理路径。
    captured_root: Path
    files: tuple[ForkSnapshotFile, ...]
    manifest_bytes: bytes
    manifest_sha256: str
    control_sha256: str
    source_lifecycle_generation: int


@dataclass(frozen=True, slots=True)
class ForkSourceSnapshot:
    """原文件与资源 ref 的冻结合同；正文仅留在既有持久化 owner 中。"""

    rollout: ForkRolloutSourceSnapshot
    attachments: tuple[ForkAttachmentSnapshot, ...] = ()
    node_debug: NodeDebugSourceCopySnapshot | None = None

    @property
    def source_session_id(self) -> str:
        return self.rollout.source_session_id

    @property
    def source_thread_id(self) -> str:
        return self.rollout.source_thread_id

    @property
    def captured_root(self) -> Path:
        return self.rollout.captured_root

    @property
    def snapshot_sha256(self) -> str:
        payload = {
            "schema": "fork-source-snapshot:v2",
            "rollout_manifest_sha256": self.rollout.manifest_sha256,
            "session_control_sha256": self.rollout.control_sha256,
            "attachments": [item.to_payload() for item in self.attachments],
            "node_debug": self.node_debug.to_payload() if self.node_debug is not None else None,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def capture_fork_rollout_snapshot(
    storage: RolloutContextStore,
    *,
    source_session_id: str,
    source_thread_id: str,
    captured_root: Path,
    snapshot_id: str,
    rollout_lock_held: bool = False,
) -> ForkRolloutSourceSnapshot:
    """复制 source rollout 与已登记普通 detail，不触碰 live SQLite reader。"""
    source_rollout = storage.storage.root(
        source_session_id,
        thread_id=source_thread_id,
    )
    if not source_rollout.is_dir() or source_rollout.is_symlink():
        raise RuntimeError(
            "source-mismatch: fork source rollout 不存在或不是普通目录"
        )
    if not snapshot_id or Path(snapshot_id).name != snapshot_id:
        raise ValueError("fork source snapshot id 必须是单一路径段")
    captured_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    snapshot_rollout = captured_root / "rollout"
    captured_root.chmod(0o700)

    source_lock_path = source_rollout.parent / ".rollout.write.lock"
    lock = None
    if not rollout_lock_held:
        lock = _RolloutFileLock(
            source_lock_path,
            exclusive=False,
        )
        lock.acquire()
    try:
        if not source_lock_path.is_file() or source_lock_path.is_symlink():
            raise RuntimeError("source-mismatch: fork source 缺少既有普通写锁文件")
        # 显式冻结副本携带既有锁，后续共享读取不得自行创建文件。
        shutil.copy2(source_lock_path, captured_root / ".rollout.write.lock")
        source_files = _tree_manifest(
            source_rollout,
            excluded_root_entries=frozenset((*SQLITE_COPY_FILES, "context-plan-details")),
        )
        shutil.copytree(source_rollout, snapshot_rollout, ignore=_copy_ignore)
        sqlite_files = copy_live_sqlite_snapshot(
            source_rollout, snapshot_rollout, rollout_lock_held=True,
        )
        index_path = snapshot_rollout / "index.sqlite"
        committed_offset = None
        if index_path.is_file():
            with closing(sqlite3.connect(index_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                row = connection.execute("SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1").fetchone()
                source_files = (*source_files, *_copy_declared_details(
                    connection, source_rollout=source_rollout, snapshot_rollout=snapshot_rollout,
                ))
            if row is None or type(row[0]) is not int or row[0] < 0:
                raise RuntimeError("source-mismatch: fork snapshot committed JSONL offset 非法")
            committed_offset = row[0]
            jsonl = snapshot_rollout / "rollout.jsonl"
            if not jsonl.is_file() or jsonl.stat().st_size < committed_offset:
                raise RuntimeError("source-mismatch: fork snapshot JSONL 短于 committed offset")
            with jsonl.open("r+b") as stream:
                stream.truncate(committed_offset)
                stream.flush()
                os.fsync(stream.fileno())
            source_files = tuple(
                ForkSnapshotFile("rollout.jsonl", committed_offset, _file_sha256(jsonl))
                if file.relative_path == "rollout.jsonl" else file for file in source_files
            )
        # live SQLite 的源 hash 由同一个隔离复制 owner 提供；父进程只读取私有副本。
        source_files = tuple(sorted(
            (*source_files, *(ForkSnapshotFile(name, size, digest) for name, size, digest in sqlite_files)),
            key=lambda file: file.relative_path,
        ))
        snapshot_files = _tree_manifest(snapshot_rollout)
        if source_files != snapshot_files:
            raise RuntimeError(
                "source-mismatch: fork source snapshot bytes 与冻结 source manifest 不一致"
            )
        copy_live_sqlite_snapshot(
            storage.storage.fork_paths.session_node(source_session_id), captured_root,
            filenames=(CONTROL_DATABASE_NAME, CONTROL_DATABASE_NAME + "-wal",
                       CONTROL_DATABASE_NAME + "-journal"),
        )
        control_path = captured_root / CONTROL_DATABASE_NAME
        if not control_path.is_file() or control_path.is_symlink():
            raise RuntimeError("source-mismatch: fork source 缺少 session-control snapshot")
        control_digest = hashlib.sha256()
        for name in (CONTROL_DATABASE_NAME, CONTROL_DATABASE_NAME + "-wal",
                     CONTROL_DATABASE_NAME + "-journal"):
            path = captured_root / name
            if path.exists():
                control_digest.update(name.encode())
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        control_digest.update(chunk)
        control_sha256 = control_digest.hexdigest()
        with closing(sqlite3.connect(control_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            owner = connection.execute(
                "SELECT kind FROM thread_catalog WHERE thread_id = ?", (source_thread_id,),
            ).fetchone()
            publication = (
                connection.execute(
                    "SELECT state FROM thread_creation_records WHERE child_thread_id = ?", (source_thread_id,),
                ).fetchone()
                if owner == ("child",) else None
            )
            fence = connection.execute(
                "SELECT state, generation FROM lifecycle_fence WHERE id = 1"
            ).fetchone()
        if (owner not in {("main",), ("child",)}
            or (owner == ("child",) and publication != ("published",))
            or fence is None or fence[0] != "active"):
            raise RuntimeError("source-mismatch: 冻结 session-control owner/fence 不一致")
        source_lifecycle_generation = fence[1]
        if type(source_lifecycle_generation) is not int or source_lifecycle_generation < 1:
            raise RuntimeError("source-mismatch: 冻结 lifecycle generation 非法")
    except BaseException:
        shutil.rmtree(captured_root, ignore_errors=True)
        raise
    finally:
        if lock is not None:
            lock.release()

    manifest_payload = {
        "schema": "fork-rollout-source-snapshot:v1",
        "snapshot_id": snapshot_id,
        "source_session_id": source_session_id,
        "source_thread_id": source_thread_id,
        "session_control_sha256": control_sha256,
        "committed_jsonl_offset": committed_offset,
        "files": [
            {
                "relative_path": file.relative_path,
                "size_bytes": file.size_bytes,
                "sha256": file.sha256,
            }
            for file in source_files
        ],
    }
    manifest_bytes = (
        json.dumps(
            manifest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    manifest_path = captured_root / "snapshot-manifest.json"
    descriptor = os.open(
        manifest_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(manifest_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(captured_root)
    return ForkRolloutSourceSnapshot(
        source_session_id=source_session_id,
        source_thread_id=source_thread_id,
        captured_root=captured_root,
        files=source_files,
        manifest_bytes=manifest_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        control_sha256=control_sha256,
        source_lifecycle_generation=source_lifecycle_generation,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_declared_details(
    connection: sqlite3.Connection, *, source_rollout: Path, snapshot_rollout: Path,
) -> tuple[ForkSnapshotFile, ...]:
    """只用本次冻结 SQLite 的 typed locator 复制正文，不解析历史业务 JSON。"""
    files: list[ForkSnapshotFile] = []
    cursor = connection.execute(
        "SELECT detail_ref,session_id,thread_id,assembly_id,relative_path "
        "FROM context_plan_details ORDER BY detail_ref",
    )
    while rows := cursor.fetchmany(256):
        for key, session_id, thread_id, assembly_id, locator in rows:
            ref = detail_ref_from_key(key)
            ref.require_owner(session_id, thread_id, assembly_id)
            require_historical_scope(connection, session_id, thread_id)
            relative = detail_relative_path(ref).relative_to("rollout")
            if locator != str(detail_relative_path(ref)):
                raise ValueError("source-mismatch: 冻结 detail locator 不属于精确历史 owner")
            source = source_rollout / relative
            for directory in (source_rollout / "context-plan-details", source.parent):
                if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
                    raise RuntimeError(f"source-mismatch: 冻结 detail 目录不是普通目录: {directory}")
            if source.is_symlink() or (source.exists() and not source.is_file()):
                raise RuntimeError(f"source-mismatch: 冻结 detail 不是普通文件: {relative}")
            if not source.is_file():
                # 是否必需由同一冻结 manifest 的资源 owner 在发布前裁定。
                continue
            destination = snapshot_rollout / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, destination)
            files.append(ForkSnapshotFile(
                relative.as_posix(), source.stat().st_size, _file_sha256(source),
            ))
    return tuple(files)


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    root = Path(directory)
    ignored = {name for name in names if name == "index.sqlite-shm"}
    if root.name == "rollout":
        ignored.update(name for name in names if name in (*SQLITE_COPY_FILES, "context-plan-details"))
    return ignored


def _tree_manifest(
    root: Path, *, excluded_root_entries: frozenset[str] = frozenset(),
) -> tuple[ForkSnapshotFile, ...]:
    files: list[ForkSnapshotFile] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*directory_names, *file_names):
            candidate = current_path / name
            if candidate.is_symlink():
                raise RuntimeError(
                    f"source-mismatch: fork source rollout 包含符号链接: {candidate}"
                )
            if name in file_names and not candidate.is_file():
                raise RuntimeError(
                    f"source-mismatch: fork source rollout 包含非普通文件: {candidate}"
                )
        directory_names[:] = [
            name
            for name in directory_names
            if not (current_path == root and name in (_PRIVATE_SOURCE_FILES | excluded_root_entries))
        ]
        for name in file_names:
            if current_path == root and name in (_PRIVATE_SOURCE_FILES | excluded_root_entries):
                continue
            path = current_path / name
            relative_path = path.relative_to(root).as_posix()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            files.append(
                ForkSnapshotFile(
                    relative_path=relative_path,
                    size_bytes=path.stat().st_size,
                    sha256=digest.hexdigest(),
                )
            )
    return tuple(sorted(files, key=lambda file: file.relative_path))



__all__ = [
    "ForkRolloutSourceSnapshot",
    "ForkSnapshotFile",
    "ForkSourceSnapshot",
    "capture_fork_rollout_snapshot",
]
