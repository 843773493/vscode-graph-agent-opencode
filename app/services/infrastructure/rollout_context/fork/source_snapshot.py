"""在 source 生命周期读 guard 内冻结 rollout 文件快照。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.services.infrastructure.rollout_context.assembly.detail_registry import (
    _COLUMNS as _DETAIL_COLUMNS,
)
from app.services.infrastructure.rollout_context.assembly.detail_registry import (
    _detail_mapping,
)
from app.services.infrastructure.rollout_context.fork.full_copy.sqlite_snapshot import (
    SQLITE_COPY_FILES,
    copy_live_sqlite_snapshot,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_record_from_mapping,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutFileLock,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage

if TYPE_CHECKING:
    from app.services.infrastructure.node_debug.session.fork import (
        NodeDebugSourceCopySnapshot,
    )
    from app.services.infrastructure.rollout_context.runtime.detail_fork import (
        ForkDetailCapability,
        ForkDetailSourceSnapshot,
    )

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
    session_node: Path
    files: tuple[ForkSnapshotFile, ...]
    manifest_bytes: bytes
    manifest_sha256: str
    control_sha256: str
    source_lifecycle_generation: int
    detail_snapshots: tuple[ForkDetailSourceSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class ForkRequestOnlyContent:
    """不落盘的 request-only 正文快照；只含稳定 ref 与 canonical JSON bytes。"""

    ref: str
    value_bytes: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.ref, str) or not self.ref:
            raise ValueError("fork request-only snapshot ref 必须是非空字符串")
        if type(self.value_bytes) is not bytes:
            raise TypeError("fork request-only snapshot 正文必须是 bytes")
        json.loads(self.value_bytes)
        from app.domain.itemized.hashing import canonical_json_bytes

        if canonical_json_bytes(json.loads(self.value_bytes)) != self.value_bytes:
            raise ValueError("fork request-only snapshot 正文必须是 canonical JSON")


@dataclass(frozen=True, slots=True)
class ForkSourceSnapshot:
    """一次 fork 的全部 source-only 临时事实，不是持久化 owner。"""

    rollout: ForkRolloutSourceSnapshot
    request_only_content: tuple[ForkRequestOnlyContent, ...] = ()
    node_debug_snapshot: NodeDebugSourceCopySnapshot | None = None

    def __post_init__(self) -> None:
        identities = [item.ref for item in self.request_only_content]
        if len(identities) != len(set(identities)):
            raise ValueError("fork request-only snapshot 含重复 ref")
        if self.node_debug_snapshot is not None and (
            self.node_debug_snapshot.source_session_id
            != self.rollout.source_session_id
            or self.node_debug_snapshot.source_thread_id
            != self.rollout.source_thread_id
        ):
            raise ValueError("fork node-debug snapshot source owner 不一致")

    @property
    def source_session_id(self) -> str:
        return self.rollout.source_session_id

    @property
    def source_thread_id(self) -> str:
        return self.rollout.source_thread_id

    @property
    def session_node(self) -> Path:
        return self.rollout.session_node

    @property
    def snapshot_sha256(self) -> str:
        """覆盖本次 capture 的 rollout、request-only 与 NodeDebug typed facts。"""
        payload: dict[str, object] = {
            "schema": "fork-source-snapshot:v1",
            "rollout_manifest_sha256": self.rollout.manifest_sha256,
            "session_control_sha256": self.rollout.control_sha256,
            "detail_content": [
                {
                    "detail_ref": item.source_record.detail_ref.to_dict(),
                    "state": "available" if item.value_bytes is not None else "unavailable",
                    "sha256": (
                        hashlib.sha256(item.value_bytes).hexdigest()
                        if item.value_bytes is not None
                        else None
                    ),
                }
                for item in self.rollout.detail_snapshots
            ],
            "request_only_content": [
                {
                    "ref": item.ref,
                    "sha256": hashlib.sha256(item.value_bytes).hexdigest(),
                }
                for item in self.request_only_content
            ],
            "node_debug": None,
        }
        if self.node_debug_snapshot is not None:
            payload["node_debug"] = {
                "manifest_sha256": self.node_debug_snapshot.manifest_sha256,
                "capture_mode": self.node_debug_snapshot.capture_mode,
                "workspace_id": self.node_debug_snapshot.workspace_id,
                "workspace_config_revision": self.node_debug_snapshot.workspace_config_revision,
                "workspace_config_hash": self.node_debug_snapshot.workspace_config_hash,
                "source_lineage": [list(item) for item in self.node_debug_snapshot.source_lineage],
                "configurations": [
                    {
                        "configuration_id": item.configuration_id,
                        "revision": item.revision,
                        "sha256": item.sha256,
                        "source_lineage": [list(lineage) for lineage in item.source_lineage],
                    }
                    for item in self.node_debug_snapshot.configuration_artifacts
                ],
            }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def freeze_request_only_content(
    values: dict[str, object],
) -> tuple[ForkRequestOnlyContent, ...]:
    """复制 Saver 的当前 request-only view，脱离可变 Python 对象引用。"""
    from app.domain.itemized.hashing import canonical_json_bytes

    return tuple(
        ForkRequestOnlyContent(ref, canonical_json_bytes(value))
        for ref, value in sorted(values.items())
    )


def capture_fork_rollout_snapshot(
    storage: RolloutStorage,
    *,
    source_session_id: str,
    source_thread_id: str,
    snapshot_session_node: Path,
    snapshot_id: str,
    detail_capability: ForkDetailCapability | None = None,
    rollout_lock_held: bool = False,
) -> ForkRolloutSourceSnapshot:
    """复制 source rollout 与已登记普通 detail，不触碰 live SQLite reader。"""
    source_rollout = storage.root(
        source_session_id,
        thread_id=source_thread_id,
    )
    if not source_rollout.is_dir() or source_rollout.is_symlink():
        raise RuntimeError(
            "source-mismatch: fork source rollout 不存在或不是普通目录"
        )
    if not snapshot_id or Path(snapshot_id).name != snapshot_id:
        raise ValueError("fork source snapshot id 必须是单一路径段")
    snapshot_session_node.mkdir(parents=True, exist_ok=False, mode=0o700)
    snapshot_rollout = snapshot_session_node / "rollout"
    snapshot_session_node.chmod(0o700)

    lock = None
    if not rollout_lock_held:
        lock = _RolloutFileLock(
            source_rollout.parent / ".rollout.write.lock",
            exclusive=False,
        )
        lock.acquire()
    try:
        source_files = _tree_manifest(
            source_rollout, excluded_root_files=frozenset(SQLITE_COPY_FILES),
        )
        shutil.copytree(source_rollout, snapshot_rollout, ignore=_copy_ignore)
        sqlite_files = copy_live_sqlite_snapshot(source_rollout, snapshot_rollout)
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
            storage._resolve_session_node(source_session_id), snapshot_session_node,
            filenames=(CONTROL_DATABASE_NAME, CONTROL_DATABASE_NAME + "-wal",
                       CONTROL_DATABASE_NAME + "-journal"),
        )
        control_path = snapshot_session_node / CONTROL_DATABASE_NAME
        if not control_path.is_file() or control_path.is_symlink():
            raise RuntimeError("source-mismatch: fork source 缺少 session-control snapshot")
        control_digest = hashlib.sha256()
        for name in (CONTROL_DATABASE_NAME, CONTROL_DATABASE_NAME + "-wal",
                     CONTROL_DATABASE_NAME + "-journal"):
            path = snapshot_session_node / name
            if path.exists():
                control_digest.update(name.encode())
                control_digest.update(path.read_bytes())
        control_sha256 = control_digest.hexdigest()
        with sqlite3.connect(control_path.as_uri() + "?mode=ro", uri=True) as connection:
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
        detail_snapshots = (
            _capture_detail_snapshots(
                snapshot_rollout,
                source_session_id=source_session_id,
                source_thread_id=source_thread_id,
                capability=detail_capability,
            )
            if detail_capability is not None
            else ()
        )
    except BaseException:
        shutil.rmtree(snapshot_session_node, ignore_errors=True)
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
        "files": [
            {
                "relative_path": file.relative_path,
                "size_bytes": file.size_bytes,
                "sha256": file.sha256,
            }
            for file in source_files
        ],
        "detail_snapshots": [
            {
                "detail_ref": snapshot.source_record.detail_ref.to_dict(),
                "state": "available" if snapshot.value_bytes is not None else "unavailable",
                "content_sha256": (
                    hashlib.sha256(snapshot.value_bytes).hexdigest()
                    if snapshot.value_bytes is not None
                    else None
                ),
            }
            for snapshot in detail_snapshots
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
    manifest_path = snapshot_session_node / "snapshot-manifest.json"
    descriptor = os.open(
        manifest_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(manifest_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(snapshot_session_node)
    return ForkRolloutSourceSnapshot(
        source_session_id=source_session_id,
        source_thread_id=source_thread_id,
        session_node=snapshot_session_node,
        files=source_files,
        manifest_bytes=manifest_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        detail_snapshots=detail_snapshots,
        control_sha256=control_sha256,
        source_lifecycle_generation=source_lifecycle_generation,
    )


def _capture_detail_snapshots(
    snapshot_rollout: Path,
    *,
    source_session_id: str,
    source_thread_id: str,
    capability: ForkDetailCapability,
) -> tuple[ForkDetailSourceSnapshot, ...]:
    index_path = snapshot_rollout / "index.sqlite"
    if index_path.is_symlink():
        raise RuntimeError("source-mismatch: fork snapshot index.sqlite 是符号链接")
    if not index_path.exists():
        return ()
    if not index_path.is_file():
        raise RuntimeError("source-mismatch: fork snapshot index.sqlite 不是普通文件")
    uri = index_path.as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_plan_details'"
        ).fetchone()
        if table is None:
            return ()
        rows = connection.execute(
            f"SELECT {','.join(_DETAIL_COLUMNS)} FROM context_plan_details "
            "WHERE session_id = ? AND thread_id = ? ORDER BY detail_ref",
            (source_session_id, source_thread_id),
        ).fetchall()
    snapshots = []
    for row in rows:
        detail = _detail_mapping(tuple(row))
        record = detail_record_from_mapping(detail)
        snapshots.append(capability.capture_source_detail(source_record=record))
    return tuple(snapshots)


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    root = Path(directory)
    ignored = {name for name in names if name == "index.sqlite-shm"}
    if root.name == "rollout":
        ignored.update(name for name in names if name in SQLITE_COPY_FILES)
    return ignored


def _tree_manifest(
    root: Path, *, excluded_root_files: frozenset[str] = frozenset(),
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
            if not (current_path == root and name in _PRIVATE_SOURCE_FILES)
        ]
        for name in file_names:
            if current_path == root and name in (_PRIVATE_SOURCE_FILES | excluded_root_files):
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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ForkRequestOnlyContent",
    "ForkRolloutSourceSnapshot",
    "ForkSnapshotFile",
    "ForkSourceSnapshot",
    "capture_fork_rollout_snapshot",
    "freeze_request_only_content",
]
