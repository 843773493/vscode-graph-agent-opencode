"""rollout storage 备份/恢复/schema 迁移/维护/启动 owner（由 RolloutStorage 组合）。

这些能力需要同时理解 JSONL durability barrier、SQLite commit chain、备份文件、读取快照
生命周期与启动自检，原分处 5 个 mixin。方法体逐字平移，只把对 host 锁/连接/路径薄壳的
self.X 调用改写为 self._host.X；SQL、事务边界、锁序与错误文案保持不变。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import (
    closing,
    contextmanager,
)
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path
from uuid import uuid4
from collections.abc import (
    Callable,
    Mapping,
    Sequence,
    Iterator,
)
from typing import TYPE_CHECKING

from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
    RolloutCheckpointIndex,
    RolloutManifest,
    _RolloutFileLock,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_text,
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    V2ItemCommitCoordinator,
    validate_commit_contract,
)
from app.core.sqlite_state import utc_now_text as _now
from app.services.infrastructure.node_debug.session.thread_owner import MAIN_THREAD_ID
from app.services.infrastructure.rollout_context.fork.node_debug_materialization import (
    publish_target_snapshot,
    remove_target_snapshot,
    verify_published_target_snapshot,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    one_of_text,
    optional_text,
    required_text,
)
from app.core.hashing import sha256_hex as _hash_bytes
from app.domain.itemized.errors import (
    FormatDispatchError,
    ItemSchemaError,
)
from app.services.infrastructure.rollout_context.storage import schema as storage_version
from app.services.infrastructure.rollout_context.storage.schema import initialize_rollout_schema
from app.services.infrastructure.rollout_context.storage.schema_upgrade import (
    RolloutSchemaUpgradeMixin,
    execute_atomic_schema_sql,
    validate_schema_journal,
)
from app.domain.itemized.enums import (
    CommitKind,
    CommitMode,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog.integrity import (
    validate_catalog_body,
    validate_projection_membership,
)
from app.services.infrastructure.rollout_context.storage.guards import mark_jsonl_commit_attempted
from app.services.infrastructure.rollout_context.storage.serialization import canonical_json_text as _json
from app.services.infrastructure.rollout_context.storage.format_dispatch import require_v2_runtime

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        _RolloutOperationLock,
    )

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )


_DEFAULT_NAMESPACE = ""

_VISIBLE_NORMAL_TURN_PREDICATE = (
    "EXISTS (SELECT 1 FROM messages AS visible_user_message "
    "WHERE visible_user_message.turn_id = t.turn_id "
    "AND visible_user_message.role = 'user' "
    "AND visible_user_message.visibility = 'visible')"
)

def reconcile_jsonl_tail(path: Path, committed_offset: int) -> None:
    """只按 SQLite committed offset 清理崩溃尾部，不扫描尾部猜测记录。"""
    committed_offset = strict_non_negative_int(
        committed_offset, field="database_meta.committed_jsonl_offset"
    )
    if not path.is_file():
        raise RuntimeError(f"committed JSONL 文件不存在: {path}")
    file_size = path.stat().st_size
    if file_size < committed_offset:
        raise RuntimeError(
            "rollout.jsonl 小于 SQLite committed offset: "
            f"file={file_size}, committed={committed_offset}"
        )
    if file_size == committed_offset:
        return
    with path.open("r+b") as stream:
        stream.truncate(committed_offset)
        stream.flush()
        os.fsync(stream.fileno())


def _turn_root_json(value: object) -> str:
    import rfc8785

    return rfc8785.dumps(value).decode("utf-8")


class RolloutMaintenanceOwner(RolloutSchemaUpgradeMixin):
    """备份/恢复/schema 迁移/维护/启动 owner；只通过 host 的锁与连接薄壳工作。"""

    def __init__(self, host: RolloutStorage) -> None:
        self._host = host


    # 继承的 RolloutSchemaUpgradeMixin 通过 self 调用 host 的锁/连接/格式薄壳；
    # owner 只转发到唯一 host，不复制第二套连接、锁或提交路径。
    def _lock(
        self,
        thread_id: str,
        checkpoint_ns: str,
        *,
        session_id: str | None = None,
    ) -> _RolloutOperationLock:
        return self._host._lock(thread_id, checkpoint_ns, session_id=session_id)

    def _connect(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        session_id: str | None = None,
        read_only: bool = False,
    ) -> sqlite3.Connection:
        return self._host._connect(
            thread_id, checkpoint_ns, session_id=session_id, read_only=read_only
        )

    def _require_v2_runtime(self, connection: sqlite3.Connection) -> None:
        self._host._require_v2_runtime(connection)

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

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _require_plain_directory(path: Path, *, field: str) -> None:
        if path.is_symlink():
            raise RuntimeError(f"{field} 不能是符号链接: {path}")
        if path.exists() and not path.is_dir():
            raise RuntimeError(f"{field} 必须是普通目录: {path}")

    def _validate_offline_restore_candidate(
        self,
        connection: sqlite3.Connection,
        *,
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
            "SELECT session_id FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if owner != (thread_id,):
            raise RuntimeError(
                "SQLite offline restore source session identity 不匹配: "
                f"expected={thread_id}, actual={owner}"
            )
        self._validate_v2_commit_offsets(
            connection,
            self._host.jsonl_path(thread_id, checkpoint_ns),
            validate_jsonl_items=True,
        )

    def _prepare_offline_restore_candidate(
        self,
        source_path: Path,
        candidate_path: Path,
        *,
        thread_id: str,
        checkpoint_ns: str,
    ) -> str:
        source_hash = self._file_hash(source_path)
        with closing(
            sqlite3.connect(
                f"{source_path.as_uri()}?mode=ro&cache=private",
                uri=True,
                timeout=0,
            )
        ) as source, closing(sqlite3.connect(candidate_path)) as candidate:
            source.execute("BEGIN")
            if source.execute("PRAGMA page_count").fetchone()[0] == 0:
                raise RuntimeError(
                    f"SQLite offline restore source 是空数据库: {source_path}"
                )
            source.backup(candidate)
            candidate.commit()
        if self._file_hash(source_path) != source_hash:
            raise RuntimeError(
                f"SQLite offline restore source 在验证期间发生变化: {source_path}"
            )
        os.chmod(candidate_path, 0o600)
        with candidate_path.open("rb") as stream:
            os.fsync(stream.fileno())
        with closing(
            sqlite3.connect(
                f"{candidate_path.as_uri()}?mode=ro&cache=private",
                uri=True,
                timeout=0,
            )
        ) as candidate:
            self._validate_offline_restore_candidate(
                candidate,
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
            )
        return source_hash

    def _require_standalone_offline_restore_source(self, source_path: Path) -> None:
        for component in source_path.parents:
            if component.is_symlink():
                raise RuntimeError(
                    "SQLite offline restore source 父目录不能是符号链接: "
                    f"{component}"
                )
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{source_path}{suffix}")
            self._require_regular_file(
                sidecar, field=f"SQLite offline restore source{suffix}"
            )
            if sidecar.exists():
                raise RuntimeError(
                    "SQLite offline restore source 必须是独立 backup artifact，"
                    f"不能依赖 sidecar: {sidecar}"
                )

    @staticmethod
    def _require_offline_restore_target(target_path: Path) -> None:
        try:
            with closing(
                sqlite3.connect(
                    f"{target_path.as_uri()}?mode=ro&cache=private",
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
        with self._host._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._host._connect(thread_id, checkpoint_ns) as connection:
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
        source_path = self._host.index_path(thread_id, checkpoint_ns)
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
        with self._host._lock(thread_id, checkpoint_ns):
            self._restore_index_backup_unlocked(thread_id, checkpoint_ns, source_path)
        return self.validate_index(thread_id, checkpoint_ns)

    def restore_index_backup_offline(
        self,
        thread_id: str,
        backup_path: str | Path,
        checkpoint_ns: str = "",
    ) -> RolloutReadSnapshot:
        """显式离线恢复完全损坏的 index，并保留原文件供审计。

        调用方必须先停止该 session 的所有非 RolloutStorage SQLite reader。
        正常启动与读取路径绝不自动调用本方法。
        """
        thread_id = strict_text(thread_id, field="offline_restore.thread_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("offline_restore.checkpoint_ns 必须是字符串")
        source_path = Path(backup_path).absolute()
        self._require_regular_file(source_path, field="SQLite offline restore source")
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        self._require_standalone_offline_restore_source(source_path)
        target_path = self._host.index_path(thread_id, checkpoint_ns)
        target_root = target_path.parent
        candidate_path = target_root / f".index.sqlite.{uuid4().hex}.offline-restore"
        operation_id = uuid4().hex
        quarantine_root = target_root / "recovery-quarantine"
        quarantine_path = quarantine_root / operation_id

        with self._host._lock(thread_id, checkpoint_ns):
            self._require_regular_file(target_path, field="SQLite offline restore target")
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
            self._require_offline_restore_target(target_path)
            moved: list[tuple[Path, Path]] = []
            installed = False
            try:
                source_hash = self._prepare_offline_restore_candidate(
                    source_path,
                    candidate_path,
                    thread_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                )
                self._require_plain_directory(
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
                    self._require_regular_file(
                        original, field="SQLite offline restore quarantined artifact"
                    )
                    if original.exists():
                        quarantined = quarantine_path / original.name
                        os.replace(original, quarantined)
                        moved.append((original, quarantined))
                self._fsync_directory(quarantine_path)
                os.replace(candidate_path, target_path)
                installed = True
                self._fsync_directory(target_root)
                with closing(
                    sqlite3.connect(
                        f"{target_path.as_uri()}?mode=ro&cache=private",
                        uri=True,
                        timeout=0,
                    )
                ) as restored:
                    self._validate_offline_restore_candidate(
                        restored,
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                    )
                manifest = {
                    "schema": "rollout-index-offline-restore:v1",
                    "operation_id": operation_id,
                    "session_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "created_at": datetime.now(UTC).isoformat(),
                    "source_path": str(source_path),
                    "source_sha256": source_hash,
                    "installed_sha256": self._file_hash(target_path),
                    "quarantined": [
                        {
                            "name": quarantined.name,
                            "sha256": self._file_hash(quarantined),
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
                self._fsync_directory(quarantine_path)
                self._fsync_directory(quarantine_root)
            except BaseException:
                if installed and target_path.exists():
                    failed_target = quarantine_path / "failed-installed-index.sqlite"
                    os.replace(target_path, failed_target)
                for original, quarantined in reversed(moved):
                    if quarantined.exists():
                        os.replace(quarantined, original)
                if quarantine_path.is_dir() and not any(quarantine_path.iterdir()):
                    quarantine_path.rmdir()
                self._fsync_directory(target_root)
                raise
            finally:
                if candidate_path.exists() or candidate_path.is_symlink():
                    candidate_path.unlink()
        return self.validate_index(thread_id, checkpoint_ns)

    def _restore_index_backup_unlocked(
        self,
        thread_id: str,
        checkpoint_ns: str,
        source_path: Path,
    ) -> None:
        """持有 rollout 写锁，通过 SQLite 事务写回原 inode，保留 reader 快照。"""
        target_path = self._host.index_path(thread_id, checkpoint_ns)
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

    def _recover_fork_materialization(
        self,
        thread_id: str,
        checkpoint_ns: str,
        connection: sqlite3.Connection,
        jsonl_path: Path,
    ) -> None:
        """恢复子 rollout 的 fork 两阶段提交日志。

        ``prepared`` 只可能指向一个尚未对外可见的目标副本，直接清空目标
        rollout；``target_committed`` 已经拥有完整的目标数据，只需补做父库
        pinned retention。这里不从 JSONL 推断任何 checkpoint 或上下文状态。
        """
        rows = connection.execute(
            """
            SELECT materialization_id, fork_id, source_session_id,
                   source_checkpoint_id, source_view_id, relationship, status
            FROM fork_materializations
            WHERE status IN ('prepared', 'target_committed')
            ORDER BY created_at
            """
        ).fetchall()
        if not rows:
            return
        if len(rows) != 1:
            raise RuntimeError(
                "fork materialization 存在多条未完成 journal，拒绝猜测恢复目标"
            )
        row = rows[0]

        (
            materialization_id,
            fork_id,
            source_session_id,
            source_checkpoint_id,
            source_view_id,
            relationship,
            status,
        ) = row
        materialization_id = required_text(
            materialization_id, field="fork_materializations.materialization_id"
        )
        fork_id = required_text(fork_id, field="fork_materializations.fork_id")
        source_session_id = required_text(
            source_session_id,
            field="fork_materializations.source_session_id",
        )
        source_checkpoint_id = optional_text(
            source_checkpoint_id,
            field="fork_materializations.source_checkpoint_id",
        )
        source_view_id = optional_text(
            source_view_id, field="fork_materializations.source_view_id"
        )
        relationship = one_of_text(
            relationship,
            {"detached", "pinned"},
            field="fork_materializations.relationship",
        )
        status = one_of_text(
            status,
            {"prepared", "target_committed"},
            field="fork_materializations.status",
        )
        if status == "target_committed":
            debug_row = connection.execute(
                "SELECT lineage_json FROM fork_identity_mappings "
                "WHERE fork_id = ? AND entity_type = 'debug_snapshot'",
                (fork_id,),
            ).fetchone()
            if debug_row is not None:
                lineage = json.loads(
                    required_text(debug_row[0], field="debug_snapshot.lineage")
                )
                if not isinstance(lineage, dict) or lineage.get("state") not in {
                    "ready",
                    "published",
                }:
                    raise RuntimeError("fork target_committed 的 debug snapshot 状态非法")
                target_node = self._host._path_resolver.resolve_session_node_for_runtime(
                    thread_id
                )
                if lineage["state"] == "ready":
                    relative = Path(
                        required_text(
                            lineage.get("target_debug_staging_path"),
                            field="debug_snapshot.target_debug_staging_path",
                        )
                    )
                    if relative.is_absolute() or ".." in relative.parts:
                        raise RuntimeError("fork debug staging journal 路径非法")
                    staging_root = target_node / relative
                    published_root = target_node / "debug" / "node"
                    if staging_root.is_dir() and not staging_root.is_symlink():
                        publish_target_snapshot(staging_root, target_node)
                    elif not published_root.is_dir() or published_root.is_symlink():
                        raise RuntimeError(
                            "fork target_committed 缺少 ready debug staging/published artifact"
                        )
                verify_published_target_snapshot(
                    target_node,
                    target_session_id=thread_id,
                    target_thread_id=MAIN_THREAD_ID,
                    manifest_sha256=required_text(
                        lineage.get("target_manifest_sha256"),
                        field="debug_snapshot.target_manifest_sha256",
                    ),
                    source_configurations_json=json.dumps(
                        lineage.get("source_configurations")
                    ),
                    configuration_id_map_json=json.dumps(
                        lineage.get("target_configuration_id_map")
                    ),
                )
                if lineage["state"] == "ready":
                    lineage["state"] = "published"
                    lineage["published_at"] = _now()
                    result = connection.execute(
                        "UPDATE fork_identity_mappings SET lineage_json = ? "
                        "WHERE fork_id = ? AND entity_type = 'debug_snapshot'",
                        (
                            json.dumps(lineage, ensure_ascii=False, sort_keys=True),
                            fork_id,
                        ),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("fork recovery 未收敛 debug published journal")
            if relationship == "pinned":
                source_root = self._host.root(source_session_id, checkpoint_ns)
                if not source_root.is_dir() or not self._host.index_path(
                    source_session_id, checkpoint_ns
                ).is_file():
                    error_message = (
                        "fork 已提交目标 rollout，但 pinned 父 rollout 不存在，"
                        "无法安全恢复 retention"
                    )
                    connection.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (_now(),),
                    )
                    connection.execute(
                        "UPDATE fork_materializations SET error_message = ? WHERE materialization_id = ?",
                        (error_message, materialization_id),
                    )
                    connection.commit()
                    raise RuntimeError(error_message)
                self._host._retain_fork_source(
                    source_session_id=source_session_id,
                    source_checkpoint_id=source_checkpoint_id,
                    source_view_id=source_view_id,
                    fork_id=fork_id,
                    owner_session_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                )
            result = connection.execute(
                "UPDATE fork_materializations SET status = 'committed', committed_at = ?, error_message = NULL WHERE materialization_id = ?",
                (_now(), materialization_id),
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"fork recovery 未完成 committed 状态收敛: {materialization_id}"
                )
            connection.commit()
            return

        connection.execute("BEGIN IMMEDIATE")
        debug_row = connection.execute(
            "SELECT lineage_json FROM fork_identity_mappings "
            "WHERE fork_id = ? AND entity_type = 'debug_snapshot'",
            (fork_id,),
        ).fetchone()
        if debug_row is not None:
            lineage = json.loads(
                required_text(debug_row[0], field="debug_snapshot.lineage")
            )
            if not isinstance(lineage, dict) or lineage.get("state") not in {
                "prepared",
                "ready",
                "published",
            }:
                raise RuntimeError("prepared fork 的 debug snapshot journal 损坏")
            target_node = self._host._path_resolver.resolve_session_node_for_runtime(thread_id)
            relative = Path(
                required_text(
                    lineage.get("target_debug_staging_path"),
                    field="debug_snapshot.target_debug_staging_path",
                )
            )
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("fork debug staging journal 路径非法")
            staging_root = target_node / relative
            published_root = target_node / "debug" / "node"
            has_staging = staging_root.exists() or staging_root.is_symlink()
            has_published = published_root.exists() or published_root.is_symlink()
            if lineage["state"] == "prepared" and not has_staging:
                raise RuntimeError("prepared fork 缺少 debug staging")
            if lineage["state"] == "ready" and has_staging == has_published:
                raise RuntimeError("ready fork 的 debug staging/published 状态不唯一")
            if lineage["state"] == "published" and not has_published:
                raise RuntimeError("published fork 缺少 debug artifact")
            if has_published:
                verify_published_target_snapshot(
                    target_node,
                    target_session_id=thread_id,
                    target_thread_id=MAIN_THREAD_ID,
                    manifest_sha256=required_text(
                        lineage.get("target_manifest_sha256"),
                        field="debug_snapshot.target_manifest_sha256",
                    ),
                    source_configurations_json=json.dumps(
                        lineage.get("source_configurations")
                    ),
                    configuration_id_map_json=json.dumps(
                        lineage.get("target_configuration_id_map")
                    ),
                )
            remove_target_snapshot(
                staging_root,
                target_node,
                remove_published=has_published,
            )
        if not jsonl_path.is_file() or jsonl_path.is_symlink():
            raise RuntimeError(
                f"prepared fork recovery 缺少安全的 JSONL 文件: {jsonl_path}"
            )
        with jsonl_path.open("r+b") as stream:
            stream.truncate(0)
            stream.flush()
            os.fsync(stream.fileno())
        for table in (
            "control_events",
            "messages",
            "message_projections",
            "item_projections",
            "tool_calls",
            "reasoning_blocks",
            "turns",
            "context_view_turns",
            "context_view_ranges",
            "context_view_jumps",
            "context_views",
            "checkpoint_channels",
            "pending_writes",
            "checkpoints",
            "branches",
            "checkpoint_namespace_state",
            "storage_commits",
            "fork_origins",
            "retention_refs",
            "item_catalog",
            "item_parts",
            "operation_anchors",
            "turn_acceptances",
            "fork_identity_mappings",
            "turn_records",
            "executions",
            "model_calls",
            "turn_execution_links",
            "context_assemblies",
            "assembly_item_refs",
            "tool_set_snapshots",
            "context_assembly_contributions",
            "context_assembly_selections",
            "context_contributions",
            "context_view_items",
            "source_overlays",
            "context_plan_details",
            "legacy_migration_reports",
        ):
            connection.execute(f"DELETE FROM {table}")
        timestamp = _now()
        result = connection.execute(
            "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, created_at, updated_at) VALUES ('branch-001', 'root', 'active', NULL, NULL, ?, ?)",
            (timestamp, timestamp),
        )
        if result.rowcount != 1:
            raise RuntimeError("fork recovery 未重建 root branch")
        result = connection.execute(
            "INSERT INTO checkpoint_namespace_state(checkpoint_ns, active_branch_id, projection_epoch, created_at, updated_at) VALUES (?, 'branch-001', 1, ?, ?)",
            (checkpoint_ns, timestamp, timestamp),
        )
        if result.rowcount != 1:
            raise RuntimeError("fork recovery 未重建 checkpoint namespace state")
        result = connection.execute(
            "UPDATE database_meta SET database_state = 'active', last_commit_id = NULL, last_message_sequence = 0, last_control_sequence = 0, committed_jsonl_offset = 0, active_branch_id = 'branch-001', projection_epoch = 1, history_view_revision = 0, source_overlay_epoch = 0, updated_at = ? WHERE singleton_id = 1",
            (timestamp,),
        )
        if result.rowcount != 1:
            raise RuntimeError("fork recovery 未重置 database_meta")
        result = connection.execute(
            "UPDATE fork_materializations SET status = 'aborted', error_message = ?, committed_at = NULL WHERE materialization_id = ?",
            ("fork 物化中断，已回滚未提交的目标 rollout", materialization_id),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"fork recovery 未标记 aborted: {materialization_id}"
            )
        connection.commit()

    def migrate_schema(
        self,
        thread_id: str,
        *,
        to_version: int,
        migration_name: str,
        migration_sql: str,
        checkpoint_ns: str = "",
    ) -> RolloutReadSnapshot:
        """执行一个事务性的 SQLite schema migration。"""
        thread_id = strict_text(thread_id, field="thread_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        to_version = strict_non_negative_int(to_version, field="to_version")
        migration_name = strict_text(migration_name, field="migration_name")
        migration_sql = strict_text(migration_sql, field="migration_sql")
        if to_version < 1:
            raise ValueError("SQLite schema version 必须从 1 开始")
        with self._host._lock(thread_id, checkpoint_ns):
            self._migrate_schema_locked(
                thread_id, checkpoint_ns=checkpoint_ns, to_version=to_version,
                migration_name=migration_name, migration_sql=migration_sql,
            )
        return self.validate_index(thread_id, checkpoint_ns)

    def _migrate_schema_locked(
        self, thread_id: str, *, checkpoint_ns: str, to_version: int,
        migration_name: str, migration_sql: str,
        validate_migrated: Callable[[sqlite3.Connection], None] | None = None,
        pending_artifact_audit: bool = False,
        allow_failed_retry: bool = False,
    ) -> None:
        """调用方持有 owner 写锁；不能在锁内获取独立读 snapshot。"""
        if pending_artifact_audit and (to_version != 3 or validate_migrated is None):
            raise ValueError("schema3 artifact 隔离必须同时提供 COMMIT 前验证")
        checksum = _hash_bytes(migration_sql.encode("utf-8"))
        with self._host._lock(thread_id, checkpoint_ns):
            with self._host._connect(thread_id, checkpoint_ns, read_only=True) as source:
                state = source.execute("SELECT database_state FROM database_meta WHERE singleton_id=1").fetchone()
                artifact_retry = (pending_artifact_audit or allow_failed_retry) and state == ("recovery_required",)
                if artifact_retry:
                    self._host._require_v2_runtime(source)
                    self._validate_schema_state(
                        source, allow_older_schema=True,
                        pending_retry=(to_version - 1, to_version, migration_name, checksum),
                    )
                    self._validate_v2_commit_offsets(source, self._host.jsonl_path(thread_id, checkpoint_ns))
            if not artifact_retry:
                self.initialize(thread_id, checkpoint_ns, _allow_schema_upgrade=True)
            with self._host._connect(thread_id, checkpoint_ns) as connection:
                row = connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if row is None:
                    raise RuntimeError("rollout database_meta 缺失")
                current_version = strict_non_negative_int(
                    row[0], field="database_meta.schema_version"
                )
            if to_version != current_version + 1:
                raise ValueError(
                    "SQLite migration 必须按版本顺序执行: "
                    f"current={current_version}, target={to_version}"
                )
            if to_version > storage_version.ROLLOUT_SCHEMA_VERSION:
                raise RuntimeError(
                    "当前程序尚未声明目标 SQLite schema 版本: "
                    f"target={to_version}, supported={storage_version.ROLLOUT_SCHEMA_VERSION}"
                )
            backup_path = self._host.root(thread_id, checkpoint_ns) / (
                f"index.sqlite.migration-{uuid4().hex}.backup"
            )
            self._backup_index_unlocked(
                thread_id,
                checkpoint_ns,
                destination=backup_path,
            )
            transaction_id = uuid4().hex
            timestamp = _now()
            try:
                with self._host._connect(thread_id, checkpoint_ns) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    migration_cursor = connection.execute(
                        "INSERT INTO schema_migrations(from_version, to_version, migration_name, migration_checksum, status, started_at) VALUES (?, ?, ?, ?, 'started', ?)",
                        (
                            current_version,
                            to_version,
                            migration_name,
                            checksum,
                            timestamp,
                        ),
                    )
                    migration_id = strict_non_negative_int(
                        migration_cursor.lastrowid,
                        field="schema_migrations.migration_id",
                    )
                    execute_atomic_schema_sql(connection, migration_sql)
                    meta_result = connection.execute(
                        "UPDATE database_meta SET schema_version = ?, updated_at = ?, "
                        "database_state = CASE WHEN ? THEN 'migrating' ELSE database_state END "
                        "WHERE singleton_id = 1",
                        (to_version, timestamp, pending_artifact_audit),
                    )
                    if meta_result.rowcount != 1:
                        raise RuntimeError("SQLite migration database_meta 更新失败")
                    migration_result = connection.execute(
                        "UPDATE schema_migrations SET status = 'completed', completed_at = ? WHERE migration_id = ?",
                        (timestamp, migration_id),
                    )
                    if migration_result.rowcount != 1:
                        raise RuntimeError("SQLite migration journal 更新失败")
                    control_sequence = self._host._insert_control(
                        connection,
                        "schema_migration",
                        "schema",
                        migration_name,
                        None,
                        None,
                        None,
                        {"from_version": current_version, "to_version": to_version},
                        transaction_id,
                        timestamp,
                    )
                    control_result = connection.execute(
                        "UPDATE database_meta SET last_control_sequence = ?, updated_at = ? WHERE singleton_id = 1",
                        (control_sequence, timestamp),
                    )
                    if control_result.rowcount != 1:
                        raise RuntimeError("SQLite migration control sequence 更新失败")
                    # 外部 artifact 已先发布；必须在 SQLite COMMIT 前校验新
                    # manifest/引用。失败进入同一回滚边界，不能先提交版本再发现损坏。
                    if artifact_retry and not pending_artifact_audit:
                        # 失败记录保留原样；只有相同合同的已验证 completed
                        # journal 允许显式升级事务解除 recovery_required。
                        connection.execute(
                            "UPDATE database_meta SET database_state='active' WHERE singleton_id=1"
                        )
                    if validate_migrated is not None:
                        validate_migrated(connection)
                    connection.commit()
            except BaseException as error:
                self._restore_index_backup_unlocked(
                    thread_id,
                    checkpoint_ns,
                    backup_path,
                )
                with self._host._connect(thread_id, checkpoint_ns) as connection:
                    failure_time = _now()
                    connection.execute(
                        "INSERT INTO schema_migrations(from_version, to_version, migration_name, migration_checksum, status, started_at, completed_at, error_message) VALUES (?, ?, ?, ?, 'failed', ?, ?, ?)",
                        (
                            current_version,
                            to_version,
                            migration_name,
                            checksum,
                            timestamp,
                            failure_time,
                            str(error),
                        ),
                    )
                    connection.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (failure_time,),
                    )
                    connection.commit()
                raise

    def _manifest_from_connection(
        self,
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        *,
        allow_migrating: bool = False,
    ) -> RolloutManifest:
        meta_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(database_meta)")
        }
        if "rollout_format_version" not in meta_columns:
            raise FormatDispatchError(
                "v1_migration_required: database_meta 缺少 rollout_format_version"
            )
        row = connection.execute(
            "SELECT rollout_id, active_branch_id, last_message_sequence, projection_epoch, last_commit_id, database_state FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("rollout database_meta 缺失")
        rollout_id = strict_text(row[0], field="database_meta.rollout_id")
        active_branch_id = strict_optional_text(
            row[1], field="database_meta.active_branch_id"
        )
        last_message_sequence = strict_non_negative_int(
            row[2], field="database_meta.last_message_sequence"
        )
        _projection_epoch = strict_non_negative_int(
            row[3], field="database_meta.projection_epoch"
        )
        _last_commit_id = strict_optional_non_negative_int(
            row[4], field="database_meta.last_commit_id"
        )
        database_state = strict_text(row[5], field="database_meta.database_state")
        if database_state != "active" and not (
            allow_migrating and database_state == "migrating"
        ):
            raise RuntimeError(f"rollout SQLite 状态不可读取: {database_state}")
        if active_branch_id is None:
            raise RuntimeError("database_meta.active_branch_id 缺失")
        namespace_state = connection.execute(
            "SELECT active_branch_id, projection_epoch FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if namespace_state is None:
            raise RuntimeError(
                f"rollout checkpoint namespace 状态缺失: {checkpoint_ns!r}"
            )
        namespace_branch_id = strict_text(
            namespace_state[0], field="checkpoint_namespace_state.active_branch_id"
        )
        namespace_projection_epoch = strict_non_negative_int(
            namespace_state[1], field="checkpoint_namespace_state.projection_epoch"
        )
        latest = connection.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE checkpoint_ns = ? AND status = 'active' ORDER BY commit_id DESC LIMIT 1",
            (checkpoint_ns,),
        ).fetchone()
        latest_checkpoint_id = (
            strict_text(latest[0], field="checkpoints.checkpoint_id")
            if latest is not None
            else None
        )
        format_row = connection.execute(
            "SELECT rollout_format_version FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if format_row is None:
            raise FormatDispatchError(
                "v1_migration_required: database_meta 缺少 rollout_format_version row"
            )
        rollout_format = strict_non_negative_int(
            format_row[0], field="database_meta.rollout_format_version"
        )
        for required_meta_column in ("history_view_revision", "source_overlay_epoch"):
            if required_meta_column not in meta_columns:
                raise RuntimeError(
                    f"v2 rollout database_meta 缺少必需字段: {required_meta_column}"
                )
        revision_row = connection.execute(
            "SELECT history_view_revision, source_overlay_epoch "
            "FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if revision_row is None:
            raise RuntimeError("v2 rollout database_meta revision row 缺失")
        history_view_revision = strict_non_negative_int(
            revision_row[0], field="database_meta.history_view_revision"
        )
        source_overlay_epoch = strict_non_negative_int(
            revision_row[1], field="database_meta.source_overlay_epoch"
        )
        from app.services.infrastructure.rollout_context.storage.primitives import (
            RolloutManifest,
        )

        return RolloutManifest(
            rollout_id,
            checkpoint_ns,
            namespace_branch_id,
            last_message_sequence,
            latest_checkpoint_id,
            namespace_projection_epoch,
            rollout_format,
            history_view_revision,
            source_overlay_epoch,
        )

    @staticmethod
    def _namespace_state(
        connection: sqlite3.Connection,
        checkpoint_ns: str,
    ) -> tuple[str, int]:
        row = connection.execute(
            "SELECT active_branch_id, projection_epoch FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"rollout checkpoint namespace 状态缺失: {checkpoint_ns!r}"
            )
        return (
            strict_text(row[0], field="checkpoint_namespace_state.active_branch_id"),
            strict_non_negative_int(
                row[1], field="checkpoint_namespace_state.projection_epoch"
            ),
        )

    def _ensure_namespace_state(
        self,
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        timestamp: str,
    ) -> None:
        existing = connection.execute(
            "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if existing is not None:
            strict_text(
                existing[0], field="checkpoint_namespace_state.active_branch_id"
            )
            return
        if checkpoint_ns == _DEFAULT_NAMESPACE:
            meta = connection.execute(
                "SELECT active_branch_id FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if meta is None or meta[0] is None:
                raise RuntimeError(
                    "rollout 默认 checkpoint namespace 缺少 active branch"
                )
            branch_id = strict_text(meta[0], field="database_meta.active_branch_id")
        else:
            branch_id = "branch-" + uuid4().hex[:12]
            branch_result = connection.execute(
                "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, created_at, updated_at) VALUES (?, 'root', 'active', NULL, NULL, ?, ?)",
                (branch_id, timestamp, timestamp),
            )
            if branch_result.rowcount != 1:
                raise RuntimeError(
                    f"rollout namespace branch 创建失败: {checkpoint_ns!r}"
                )
        namespace_result = connection.execute(
            "INSERT INTO checkpoint_namespace_state(checkpoint_ns, active_branch_id, projection_epoch, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
            (checkpoint_ns, branch_id, timestamp, timestamp),
        )
        if namespace_result.rowcount != 1:
            raise RuntimeError(f"rollout namespace state 创建失败: {checkpoint_ns!r}")

    def _initialize_schema(self, thread_id: str, checkpoint_ns: str) -> None:
        del checkpoint_ns
        with self._host._connect(thread_id) as connection:
            initialize_rollout_schema(connection)

    def _commit_connection(self, connection: sqlite3.Connection) -> None:
        """提交已经完成 JSONL durability barrier 的 SQLite 事务。"""
        connection.commit()
        mark_jsonl_commit_attempted(connection)

    @staticmethod
    def _is_removed_rollout_layout(root: Path) -> bool:
        """判断目录是否仍是已经移除的旧 rollout 布局。"""
        return (root / "manifest.json").exists() or any(root.glob("segment-*.jsonl"))

    @staticmethod
    def _validate_schema_state(
        connection: sqlite3.Connection, *, allow_older_schema: bool = False,
        pending_retry: tuple[int, int, str, str | None] | None = None,
    ) -> None:
        removed_journal = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'compaction_runs'"
        ).fetchone()
        if removed_journal is not None and connection.execute(
            "SELECT 1 FROM compaction_runs LIMIT 1"
        ).fetchone() is not None:
            raise RuntimeError(
                "recovery-required: 检测到已移除的物理 compaction journal；"
                "保留原始 JSONL/SQLite/备份，禁止自动替换已提交事实"
            )
        row = connection.execute(
            "SELECT schema_version, message_format_version FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("rollout database_meta 缺失")
        schema_version = strict_non_negative_int(
            row[0], field="database_meta.schema_version"
        )
        message_format_version = strict_non_negative_int(
            row[1], field="database_meta.message_format_version"
        )
        if schema_version > storage_version.ROLLOUT_SCHEMA_VERSION:
            raise RuntimeError(
                "rollout SQLite schema 版本高于当前程序支持范围: "
                f"database={row[0]}, supported={storage_version.ROLLOUT_SCHEMA_VERSION}"
            )
        if schema_version < storage_version.ROLLOUT_SCHEMA_VERSION and not allow_older_schema:
            raise RuntimeError(
                "schema-upgrade-required: v2 SQLite 必须显式执行 Saver.upgrade_rollout_schema，"
                f"current={schema_version}, target={storage_version.ROLLOUT_SCHEMA_VERSION}"
            )
        if message_format_version != storage_version.MESSAGE_FORMAT_VERSION:
            raise RuntimeError(
                "rollout JSONL message format 版本不受支持: "
                f"database={row[1]}, supported={storage_version.MESSAGE_FORMAT_VERSION}"
            )
        if pending_retry is not None and not allow_older_schema:
            raise RuntimeError("schema-upgrade-retry-conflict: 普通 runtime 不允许失败迁移重试")
        validate_schema_journal(connection, schema_version, pending_retry=pending_retry)

    @staticmethod
    def _validate_v2_commit_offsets(
        connection: sqlite3.Connection,
        jsonl_path: Path,
        *,
        validate_jsonl_items: bool = True,
    ) -> None:
        """校验 committed offset 的单一权威和 storage commit 链。"""
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(database_meta)")
        }
        if "rollout_format_version" not in columns:
            return
        row = connection.execute(
            "SELECT rollout_format_version, committed_jsonl_offset, last_commit_id "
            "FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None or strict_non_negative_int(
            row[0], field="database_meta.rollout_format_version"
        ) != storage_version.ROLLOUT_FORMAT_VERSION:
            return
        database_offset = strict_non_negative_int(
            row[1], field="database_meta.committed_jsonl_offset"
        )
        database_last_commit_id = strict_optional_non_negative_int(
            row[2], field="database_meta.last_commit_id"
        )
        jsonl_size = jsonl_path.stat().st_size
        commits = connection.execute(
            "SELECT commit_id, jsonl_start_offset, jsonl_end_offset, "
            "jsonl_offset_before, jsonl_offset_after, jsonl_record_count, status, "
            "commit_kind, commit_mode, outcome, metadata_json, jsonl_fsync_at "
            "FROM storage_commits ORDER BY commit_id"
        ).fetchall()
        previous = 0
        commit_kinds = {value.value for value in CommitKind}
        commit_modes = {value.value for value in CommitMode}
        for commit in commits:
            commit_id = strict_non_negative_int(
                commit[0], field="storage_commits.commit_id"
            )
            start = strict_non_negative_int(
                commit[1], field=f"storage commit jsonl_start_offset: {commit_id}"
            )
            end = strict_non_negative_int(
                commit[2], field=f"storage commit jsonl_end_offset: {commit_id}"
            )
            before = strict_non_negative_int(
                commit[3], field=f"storage commit jsonl_start_offset: {commit_id}"
            )
            end_offset = strict_non_negative_int(
                commit[4], field=f"storage commit jsonl_offset_after: {commit_id}"
            )
            record_count = strict_non_negative_int(
                commit[5], field=f"storage commit jsonl_record_count: {commit_id}"
            )
            status = strict_text(commit[6], field=f"storage commit status: {commit_id}")
            if status != "committed":
                raise RuntimeError(f"v2 storage commit 未收敛: commit_id={commit_id}")
            commit_kind = strict_text(
                commit[7], field=f"storage commit kind: {commit_id}"
            )
            commit_mode = strict_text(
                commit[8], field=f"storage commit mode: {commit_id}"
            )
            if commit_kind not in commit_kinds:
                raise RuntimeError(
                    f"v2 storage commit kind 非法: commit_id={commit_id}, kind={commit_kind}"
                )
            if commit_mode not in commit_modes:
                raise RuntimeError(
                    f"v2 storage commit mode 非法: commit_id={commit_id}, mode={commit_mode}"
                )
            after = strict_non_negative_int(
                commit[4], field=f"storage commit jsonl_offset_after: {commit_id}"
            )
            if end_offset != after:
                raise RuntimeError(
                    f"v2 storage commit end offset 字段不一致: commit_id={commit_id}"
                )
            outcome = commit[9]
            if outcome is not None and not isinstance(outcome, str):
                raise RuntimeError(
                    f"v2 storage commit outcome 必须是字符串或 null: commit_id={commit_id}"
                )
            try:
                validate_commit_contract(
                    commit_kind=commit_kind,
                    commit_mode=commit_mode,
                    item_count=record_count,
                    outcome=outcome,
                )
            except (ItemSchemaError, TypeError, ValueError) as error:
                raise RuntimeError(
                    f"v2 storage commit contract 非法: commit_id={commit_id}: {error}"
                ) from error
            metadata_json = commit[10]
            if not isinstance(metadata_json, str) or not metadata_json:
                raise RuntimeError(
                    f"v2 storage commit metadata_json 必须是非空字符串: commit_id={commit_id}"
                )
            try:
                metadata_value = json.loads(metadata_json)
            except (TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"v2 storage commit metadata_json 非法: commit_id={commit_id}"
                ) from error
            if not isinstance(metadata_value, Mapping) or _json(metadata_value) != metadata_json:
                raise RuntimeError(
                    "v2 storage commit metadata_json 不是 RFC 8785 JCS object: "
                    f"commit_id={commit_id}"
                )
            if "physical_record_count" in metadata_value or "compacted" in metadata_value:
                raise RuntimeError(
                    "immutable JSONL 不允许已提交记录被物理压缩或重写: "
                    f"commit_id={commit_id}"
                )
            if start != before or end != after:
                raise RuntimeError(
                    "v2 storage commit JSONL offset 链断裂（offset 字段不一致）: "
                    f"commit_id={commit_id}, before={before}, start={start}, "
                    f"end={end}, after={after}"
                )
            if before != previous or after < before:
                raise RuntimeError(
                    "v2 storage commit JSONL offset 链断裂: "
                    f"commit_id={commit_id}, previous={previous}, before={before}, "
                    f"start={start}, end={end}, after={after}"
                )
            if (
                commit_mode == CommitMode.ITEM_BEARING.value
                and not isinstance(commit[11], str)
            ):
                raise RuntimeError(
                    f"item-bearing commit 缺少 JSONL fsync barrier: commit_id={commit_id}"
                )
            if commit[11] is not None and (
                not isinstance(commit[11], str) or not commit[11]
            ):
                raise RuntimeError(
                    f"v2 storage commit fsync timestamp 非法: commit_id={commit_id}"
                )
            if commit_mode == CommitMode.METADATA_ONLY.value and (
                start != end or record_count != 0
            ):
                raise RuntimeError(
                    f"metadata-only commit 不得推进 JSONL offset: commit_id={commit_id}"
                )
            if commit_mode == CommitMode.ITEM_BEARING.value and record_count <= 0:
                raise RuntimeError(
                    f"item-bearing commit 必须包含 item: commit_id={commit_id}"
                )
            catalog_row = connection.execute(
                "SELECT COUNT(*) FROM item_catalog WHERE commit_id = ?",
                (commit_id,),
            ).fetchone()
            catalog_count = strict_non_negative_int(
                catalog_row[0], field=f"item catalog count: {commit_id}"
            )
            if catalog_count != record_count:
                raise RuntimeError(
                    "storage commit 的 item catalog 数量不一致: "
                    f"commit_id={commit_id}, catalog={catalog_count}, "
                    f"record_count={record_count}"
                )
            previous = after
        if commits and database_last_commit_id != strict_non_negative_int(
            commits[-1][0], field="storage_commits.last_commit_id"
        ):
            raise RuntimeError(
                "database_meta.last_commit_id 与 storage_commits 不一致: "
                f"meta={database_last_commit_id}, commits={commits[-1][0]}"
            )
        if not commits and database_last_commit_id is not None:
            raise RuntimeError(
                "database_meta.last_commit_id 指向不存在的 storage commit: "
                f"{database_last_commit_id}"
            )
        # 即使调用方省略逐 item 正文校验，commit chain 仍必须与 meta
        # 等值。尾部尚未收敛的字节可以存在，但不得用于选择另一边界。
        if previous != database_offset or jsonl_size < database_offset:
            raise RuntimeError(
                "database_meta.committed_jsonl_offset 与 storage_commits 不一致: "
                f"meta={database_offset}, commits={previous}, file={jsonl_size}"
            )
        jsonl_bytes = b""
        if validate_jsonl_items:
            with jsonl_path.open("rb") as stream:
                jsonl_bytes = stream.read(database_offset)

        item_rows = connection.execute(
            "SELECT item_sequence, item_id, content_hash, jsonl_offset, jsonl_length, "
            "commit_id, payload_length, source_revision FROM item_catalog ORDER BY jsonl_offset"
        ).fetchall()
        expected_item_offset = 0
        commit_ranges = {
            strict_non_negative_int(commit[0], field="storage commit id"): (
                strict_non_negative_int(commit[1], field="storage commit start offset"),
                strict_non_negative_int(commit[2], field="storage commit end offset"),
                strict_non_negative_int(commit[5], field="storage commit record count"),
            )
            for commit in commits
        }
        expected_item_sequence = 1
        for (
            item_sequence,
            item_id,
            catalog_hash,
            item_offset,
            item_length,
            item_commit_id,
            logical_length,
            source_revision,
        ) in item_rows:
            item_id = strict_text(item_id, field="item_catalog.item_id")
            logical_length = strict_non_negative_int(
                logical_length, field=f"item_catalog.payload_length: {item_id}"
            )
            source_revision = strict_text(
                source_revision, field=f"item_catalog.source_revision: {item_id}"
            )
            sequence = strict_non_negative_int(
                item_sequence, field=f"item_catalog.item_sequence: {item_id}"
            )
            offset = strict_non_negative_int(
                item_offset, field=f"item_catalog.jsonl_offset: {item_id}"
            )
            length = strict_non_negative_int(
                item_length, field=f"item_catalog.jsonl_length: {item_id}"
            )
            catalog_hash = strict_text(
                catalog_hash,
                field=f"item_catalog.content_hash: {item_id}",
            )
            if length == 0:
                raise RuntimeError(
                    f"item_catalog.jsonl_length 必须大于 0: {item_id}"
                )
            if sequence != expected_item_sequence:
                raise RuntimeError(
                    "v2 item catalog item_sequence 不连续或顺序非法，拒绝使用派生索引: "
                    f"item_id={item_id}, sequence={sequence}, "
                    f"expected={expected_item_sequence}"
                )
            if offset != expected_item_offset or length <= 0:
                raise RuntimeError(
                    "v2 item catalog JSONL locator 不连续或非法: "
                    f"item_id={item_id}, sequence={sequence}, offset={offset}, "
                    f"length={length}, expected_offset={expected_item_offset}"
                )
            end = offset + length
            if end > database_offset:
                raise RuntimeError(
                    f"v2 item catalog JSONL locator 越界: item_id={item_id}"
                )
            if validate_jsonl_items:
                validate_catalog_body(
                    jsonl_bytes[offset:end],
                    sequence=sequence,
                    item_id=item_id,
                    catalog_hash=catalog_hash,
                    payload_length=logical_length,
                    source_revision=source_revision,
                )
            item_commit_id_value = strict_non_negative_int(
                item_commit_id, field=f"item_catalog.commit_id: {item_id}"
            )
            commit_range = commit_ranges.get(item_commit_id_value)
            if commit_range is None:
                raise RuntimeError(
                    f"v2 item catalog 指向不存在的 storage commit: item_id={item_id}"
                )
            commit_start, commit_end, commit_record_count = commit_range
            if (
                item_offset < commit_start
                or end > commit_end
                or commit_record_count <= 0
            ):
                raise RuntimeError(
                    "storage commit 的 item catalog offset 不在 commit 边界内: "
                    f"commit_id={item_commit_id_value}, item_id={item_id}, "
                    f"item_start={item_offset}, item_end={end}, "
                    f"start={commit_start}, end={commit_end}"
                )
            expected_item_offset = end
            expected_item_sequence += 1
        if expected_item_offset != database_offset:
            raise RuntimeError(
                "database_meta.committed_jsonl_offset 与 storage_commits 不一致: "
                f"meta={database_offset}, commits={previous}, "
                f"catalog_end={expected_item_offset}, file={jsonl_size}"
            )
        validate_projection_membership(connection)

    def open_read_snapshot(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        validate_integrity: bool = False,
    ) -> RolloutReadSnapshot:
        """打开带文件锁和 SQLite 事务的只读 snapshot。"""
        root = self._host.root(thread_id, checkpoint_ns)
        jsonl_path = self._host.jsonl_path(thread_id, checkpoint_ns)
        index_path = self._host.index_path(thread_id, checkpoint_ns)
        if not root.is_dir() or not jsonl_path.is_file() or not index_path.is_file():
            self.initialize(thread_id, checkpoint_ns)
        file_lock = _RolloutFileLock(root.parent / ".rollout.write.lock", exclusive=False)
        file_lock.acquire()
        connection: sqlite3.Connection | None = None
        try:
            connection = self._host._connect(thread_id, checkpoint_ns, read_only=True)
            self._validate_schema_state(connection)
            self._host._validate_reasoning_projection_connection(connection)
            self._host._require_v2_runtime(connection)
            self._validate_v2_commit_offsets(
                connection, jsonl_path, validate_jsonl_items=validate_integrity
            )
            database_state = connection.execute(
                "SELECT database_state FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if database_state is None or (
                strict_text(database_state[0], field="database_meta.database_state")
                == "migrating"
            ):
                raise RuntimeError(
                    "rollout 正在进行 legacy migration，暂不可建立业务 context snapshot"
                )
            committed_row = connection.execute(
                "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if committed_row is None:
                raise RuntimeError("database_meta 缺少 committed_jsonl_offset")
            committed_offset = strict_non_negative_int(
                committed_row[0],
                field="database_meta.committed_jsonl_offset",
            )
            if jsonl_path.stat().st_size < committed_offset:
                raise RuntimeError("rollout.jsonl 小于 SQLite 已提交偏移，无法安全恢复")
            integrity = (
                connection.execute("PRAGMA integrity_check").fetchone()[0]
                if validate_integrity
                else "ok"
            )
            if integrity != "ok":
                connection.rollback()
                connection.close()
                file_lock.release()
                with (
                    self._host._lock(thread_id, checkpoint_ns),
                    self._host._connect(thread_id, checkpoint_ns) as writable,
                ):
                    writable.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (_now(),),
                    )
                raise RuntimeError(
                    "rollout SQLite integrity_check 失败: "
                    f"{self._host.index_path(thread_id, checkpoint_ns)}: {integrity}"
                )
            connection.execute("BEGIN")
            manifest = self._manifest_from_connection(connection, checkpoint_ns)
        except sqlite3.DatabaseError as error:
            if connection is not None:
                connection.close()
            file_lock.release()
            raise RuntimeError(
                "recovery_required: rollout SQLite 无法读取；"
                "必须从已验证的 SQLite backup 执行显式恢复，禁止从 JSONL 重建: "
                f"{index_path}"
            ) from error
        except Exception:
            if connection is not None:
                connection.close()
            file_lock.release()
            raise
        return RolloutReadSnapshot(
            thread_id,
            checkpoint_ns,
            manifest,
            connection,
            file_lock,
        )

    def validate_index(self, thread_id: str, checkpoint_ns: str = "") -> RolloutReadSnapshot:
        """执行完整 SQLite integrity_check。"""
        return self.open_read_snapshot(thread_id, checkpoint_ns, validate_integrity=True)

    def repair_index(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        manifest: RolloutManifest | None = None,
    ) -> None:
        del manifest
        raise RuntimeError(
            "SQLite 是 rollout 控制状态的权威来源，不能从 rollout.jsonl 重建 index；请恢复 SQLite 备份"
        )

    def _append_v2_records_transaction(
        self,
        connection: sqlite3.Connection,
        thread_id: str,
        checkpoint_ns: str,
        items: Sequence[CanonicalItemRecord],
        *,
        commit_kind: str,
        commit_mode: str | None = None,
        outcome: str | None = None,
        metadata: Mapping[str, object] | None = None,
        subject_id: str | None = None,
        idempotency_key: str | None = None,
        begin_transaction: bool = True,
    ) -> tuple[int, int]:
        """通过统一 coordinator 执行 v2 item-bearing 原子提交。"""
        coordinator = V2ItemCommitCoordinator(
            jsonl_path=self._host.jsonl_path(thread_id, checkpoint_ns),
            canonical_writer=self._host._insert_canonical_item,
        )
        return coordinator.append(
            connection,
            items,
            commit_kind=commit_kind,
            commit_mode=commit_mode,
            outcome=outcome,
            metadata=metadata,
            subject_id=subject_id,
            idempotency_key=idempotency_key,
            begin_transaction=begin_transaction,
        )

    def _checkpoint_index(self, row: Sequence[object]) -> RolloutCheckpointIndex:
        if len(row) != 16:
            raise RuntimeError(
                f"checkpoint index 列数非法: expected=16, got={len(row)}"
            )
        blobs = (row[13], row[15])
        if any(not isinstance(blob, bytes) for blob in blobs):
            raise RuntimeError("checkpoint index blob 字段必须是 bytes")
        return RolloutCheckpointIndex(
            strict_text(row[0], field="checkpoints.checkpoint_id"),
            strict_text(row[1], field="checkpoints.checkpoint_ns", allow_empty=True),
            strict_non_negative_int(row[2], field="checkpoints.commit_id"),
            strict_non_negative_int(
                row[3], field="checkpoints.message_sequence"
            ),
            strict_non_negative_int(row[4], field="checkpoints.message_count"),
            strict_optional_text(
                row[5], field="checkpoints.parent_checkpoint_id"
            ),
            strict_text(row[6], field="checkpoints.view_id"),
            strict_text(row[7], field="checkpoints.branch_id"),
            strict_non_negative_int(
                row[8], field="checkpoints.checkpoint_version"
            ),
            strict_text(row[9], field="checkpoints.checkpoint_timestamp"),
            strict_text(row[10], field="checkpoints.checkpoint_json"),
            strict_text(row[11], field="checkpoints.metadata_json"),
            strict_text(row[12], field="checkpoints.versions_seen_type"),
            blobs[0],
            strict_text(row[14], field="checkpoints.pending_sends_type"),
            blobs[1],
        )

    def rollout_id(self, thread_id: str, checkpoint_ns: str = "") -> str:
        manifest = self.initialize(thread_id, checkpoint_ns)
        with self._host._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._host._require_v2_runtime(connection)
        return manifest.rollout_id

    @contextmanager
    def _existing_index_connection(
        self,
        thread_id: str,
        checkpoint_ns: str,
    ) -> Iterator[sqlite3.Connection]:
        """读取既有 authority；完全损坏时只报告恢复要求，不创建新库。"""
        try:
            with self._host._connect(
                thread_id,
                checkpoint_ns,
                read_only=True,
            ) as connection:
                yield connection
        except sqlite3.DatabaseError as error:
            raise RuntimeError(
                "recovery_required: rollout SQLite 无法读取；"
                "必须从已验证的 SQLite backup 执行显式恢复，禁止从 JSONL 重建: "
                f"{self._host.index_path(thread_id, checkpoint_ns)}"
            ) from error

    def initialize(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        validate_jsonl_items: bool = True,
        _allow_schema_upgrade: bool = False,
    ) -> RolloutManifest:
        thread_id = strict_text(thread_id, field="thread_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        with self._host._lock(thread_id, checkpoint_ns):
            root = self._host.root(thread_id, checkpoint_ns)
            root.mkdir(parents=True, exist_ok=True)
            if self._is_removed_rollout_layout(root):
                raise RuntimeError(f"rollout 使用了已移除的旧布局: {root}")
            # 正常 runtime 的初始化不能把已有 v1 source 当作空库升级或
            # fallback。v1 原件只能由显式 legacy_import_v1_to_v2 只读读取，
            # 迁移目标则必须是新建/空的 v2 rollout。
            existing_index = self._host.index_path(thread_id, checkpoint_ns)
            needs_schema = True
            if existing_index.is_file() and existing_index.stat().st_size > 0:
                with self._existing_index_connection(
                    thread_id,
                    checkpoint_ns,
                ) as existing_connection:
                    meta_columns = {
                        str(row[1])
                        for row in existing_connection.execute(
                            "PRAGMA table_info(database_meta)"
                        )
                    }
                    if "rollout_format_version" not in meta_columns:
                        raise FormatDispatchError(
                            "v1_migration_required: rollout 缺少 v2 format dispatch"
                        )
                    format_row = existing_connection.execute(
                        "SELECT rollout_format_version FROM database_meta WHERE singleton_id = 1"
                    ).fetchone()
                    if format_row is None:
                        # schema.py 先创建空的 v2 表，再由本方法原子创建
                        # database_meta。这个窗口不是 v1 artifact；只有已经
                        # 存在业务行却没有 v2 meta 时才是不可恢复的半成品。
                        v2_rows = sum(
                            strict_non_negative_int(
                                existing_connection.execute(
                                    f"SELECT COUNT(*) FROM {table}"
                                ).fetchone()[0],
                                field=f"{table}.count",
                            )
                            for table in (
                                "messages",
                                "item_catalog",
                                "storage_commits",
                                "turn_records",
                                "context_views",
                                "checkpoints",
                                "context_plans",
                                "context_plan_refs",
                                "context_plan_contributions",
                                "context_plan_seal_failures",
                                "tool_set_snapshots",
                            )
                        )
                        if v2_rows:
                            raise RuntimeError(
                                "v2 rollout database_meta 缺失但已有业务数据，"
                                "拒绝猜测半成品恢复: v2_migration_repair_required"
                            )
                        if "rollout_format_version" not in meta_columns:
                            raise FormatDispatchError(
                                "v1_migration_required: rollout database_meta 缺少 format row"
                            )
                        # 空的、已创建 v2 schema 继续走下面的 meta 初始化。
                    else:
                        needs_schema = False
                        require_v2_runtime(
                            strict_non_negative_int(
                                format_row[0],
                                field="database_meta.rollout_format_version",
                            )
                        )
                        self._validate_schema_state(
                            existing_connection, allow_older_schema=_allow_schema_upgrade
                        )
            path = self._host.jsonl_path(thread_id, checkpoint_ns)
            # 只读历史请求会频繁经过 initialize；已有文件不能重复 touch，
            # 否则会改变 rollout.jsonl 的 mtime，触发工作区文件监听并造成
            # 无意义的资源刷新。首次创建时才建立空的 canonical 文件。
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise RuntimeError(f"rollout.jsonl 不是安全普通文件: {path}")
            if not path.exists():
                path.touch()
            # 已有 authoritative meta 的索引只能验证；建表/改表由显式 schema migration 负责。
            if needs_schema:
                self._initialize_schema(thread_id, checkpoint_ns)
            self._host._validate_reasoning_projection_schema(thread_id, checkpoint_ns)
            if not self._host._legacy_migration_is_active((thread_id, checkpoint_ns)):
                self._reject_unpublished_migration(thread_id, checkpoint_ns)
            with self._host._connect(thread_id, checkpoint_ns) as connection:
                meta = connection.execute(
                    "SELECT * FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if meta is None:
                    rollout_id = self._host._rollout_id(thread_id)
                    timestamp = _now()
                    meta_result = connection.execute(
                        """INSERT INTO database_meta(singleton_id, rollout_id, session_id,
                            schema_version, message_format_version, database_state,
                            last_message_sequence, last_control_sequence, committed_jsonl_offset,
                            projection_epoch, created_at, updated_at, rollout_format_version)
                            VALUES (1, ?, ?, ?, ?, 'active', 0, 0, 0, 1, ?, ?, ?)""",
                        (
                            rollout_id,
                            thread_id,
                            storage_version.ROLLOUT_SCHEMA_VERSION,
                            storage_version.MESSAGE_FORMAT_VERSION,
                            timestamp,
                            timestamp,
                            storage_version.ROLLOUT_FORMAT_VERSION,
                        ),
                    )
                    if meta_result.rowcount != 1:
                        raise RuntimeError("rollout database_meta 创建失败")
                    branch_result = connection.execute(
                        "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, created_at, updated_at) VALUES (?, 'root', 'active', NULL, NULL, ?, ?)",
                        ("branch-001", timestamp, timestamp),
                    )
                    if branch_result.rowcount != 1:
                        raise RuntimeError("rollout root branch 创建失败")
                    branch_meta_result = connection.execute(
                        "UPDATE database_meta SET active_branch_id = 'branch-001' WHERE singleton_id = 1"
                    )
                    if branch_meta_result.rowcount != 1:
                        raise RuntimeError("rollout active branch 写入失败")
                    migration_result = connection.execute(
                        "INSERT INTO schema_migrations(from_version, to_version, migration_name, migration_checksum, status, started_at, completed_at) VALUES (0, ?, ?, ?, 'completed', ?, ?)",
                        (
                            storage_version.ROLLOUT_SCHEMA_VERSION,
                            f"rollout_sqlite_v{storage_version.ROLLOUT_SCHEMA_VERSION}",
                            _hash_bytes(f"rollout_sqlite_v{storage_version.ROLLOUT_SCHEMA_VERSION}".encode()),
                            timestamp,
                            timestamp,
                        ),
                    )
                    if migration_result.rowcount != 1:
                        raise RuntimeError("rollout schema migration journal 创建失败")
                self._ensure_namespace_state(connection, checkpoint_ns, _now())
                active_branch = connection.execute(
                    "SELECT branch_id, head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?) AND status = 'active'",
                    (checkpoint_ns,),
                ).fetchone()
                if active_branch is None:
                    raise RuntimeError(
                        "active branch 缺失，不能创建 rollout context view"
                    )
                active_branch_id = strict_text(
                    active_branch[0], field="branches.branch_id"
                )
                active_head_view_id = strict_optional_text(
                    active_branch[1], field="branches.head_view_id"
                )
                if active_head_view_id is None:
                    # acceptance 可以先于第一个 LangGraph checkpoint 到达；
                    # 为这个空但真实存在的 active branch 建立 root view，
                    # 使 Turn/root 在 acceptance-time 就有稳定的 view-local
                    # 索引，而不是等下一次 checkpoint 偶然补齐。
                    initial_view_id = self._host._create_view(
                        connection,
                        active_branch_id,
                        None,
                        (),
                        _now(),
                        view_kind="root",
                    )
                    head_result = connection.execute(
                        "UPDATE branches SET head_view_id = ?, updated_at = ? WHERE branch_id = ?",
                        (initial_view_id, _now(), active_branch_id),
                    )
                    if head_result.rowcount != 1:
                        raise RuntimeError(
                            f"rollout active branch head view 更新失败: {active_branch_id}"
                        )
                self._validate_schema_state(connection, allow_older_schema=_allow_schema_upgrade)
                if (thread_id, checkpoint_ns) not in self._host._active_fork_materializations:
                    self._recover_fork_materialization(
                        thread_id,
                        checkpoint_ns,
                        connection,
                        path,
                    )
                committed_offset_row = connection.execute(
                    "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if committed_offset_row is None:
                    raise RuntimeError("rollout database_meta committed offset 缺失")
                committed_offset = strict_non_negative_int(
                    committed_offset_row[0],
                    field="database_meta.committed_jsonl_offset",
                )
                file_size = path.stat().st_size
                if file_size < committed_offset:
                    result = connection.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (_now(),),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("rollout recovery_required 状态写入失败")
                    raise TypeError(
                        "rollout.jsonl 小于 SQLite 已提交偏移，无法安全恢复"
                    )
                # 先验证 meta、提交链及 catalog 的同一边界；损坏的 meta
                # 不能成为截断依据，否则会在报错前删除已提交 item。
                self._validate_v2_commit_offsets(
                    connection,
                    path,
                    validate_jsonl_items=validate_jsonl_items,
                )
                if file_size > committed_offset:
                    reconcile_jsonl_tail(path, committed_offset)
                return self._manifest_from_connection(
                    connection,
                    checkpoint_ns,
                    allow_migrating=self._host._legacy_migration_is_active(
                        (thread_id, checkpoint_ns)
                    ),
                )

    def _reject_unpublished_migration(
        self, thread_id: str, checkpoint_ns: str,
    ) -> None:
        """正常 runtime 不清空迁移半成品；恢复只由显式 migration owner 执行。"""
        with self._host._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            row = connection.execute(
                "SELECT database_state FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
        if row is not None and row[0] == "migrating":
            raise RuntimeError(
                "migration-installation-incomplete: target 尚未原子安装；"
                "保留原始 JSONL/SQLite，必须由显式 legacy migration 恢复审计"
            )

    def repair_active_context_view(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """修复 active view 的 Turn 索引，不重写 canonical 消息文件。

        旧版本按全局消息序号连续性判断 Turn 完整性。并发执行时不同 Turn
        的消息会交错，导致 view 的消息范围存在但 ``context_view_turns`` 被
        错误删空。这里依据每个 Turn 自身的消息集合重新计算索引；只有索引
        与规范结果不一致时才写 SQLite，避免普通只读请求产生文件监听噪声。
        """
        with self._host._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._host._connect(thread_id, checkpoint_ns) as connection:
                self._host._require_v2_runtime(connection)
                namespace = self._namespace_state(connection, checkpoint_ns)
                branch_row = connection.execute(
                    "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
                    (namespace[0],),
                ).fetchone()
                if branch_row is None:
                    return False
                view_id = strict_optional_text(
                    branch_row[0], field="branches.head_view_id"
                )
                if view_id is None:
                    return False
                visible_sequences = set(
                    self._host._view_message_sequences_from_connection(connection, view_id)
                )
                expected: list[tuple[str, int, int | None, int | None]] = []
                turn_rows = connection.execute(
                    f"SELECT turn_id, first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence FROM turns AS t WHERE t.turn_kind = 'normal' AND t.user_message_sequence IS NOT NULL AND {_VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY t.turn_ordinal"
                ).fetchall()
                for row in turn_rows:
                    turn_id = strict_text(row[0], field="turns.turn_id")
                    first_sequence = strict_non_negative_int(
                        row[1], field=f"turns.first_message_sequence:{turn_id}"
                    )
                    last_sequence = strict_non_negative_int(
                        row[2], field=f"turns.last_message_sequence:{turn_id}"
                    )
                    user_sequence = strict_optional_non_negative_int(
                        row[3], field=f"turns.user_message_sequence:{turn_id}"
                    )
                    final_sequence = strict_optional_non_negative_int(
                        row[4], field=f"turns.final_message_sequence:{turn_id}"
                    )
                    if (
                        first_sequence == 0
                        or last_sequence == 0
                        or last_sequence < first_sequence
                        or user_sequence is None
                    ):
                        raise RuntimeError(
                            f"active view repair Turn message range 非法: {turn_id}"
                        )
                    turn_sequences = {
                        strict_non_negative_int(
                            message_row[0],
                            field=f"messages.message_sequence:{turn_id}",
                        )
                        for message_row in connection.execute(
                            "SELECT message_sequence FROM messages WHERE turn_id = ?",
                            (turn_id,),
                        ).fetchall()
                    }
                    if not turn_sequences:
                        raise RuntimeError(
                            f"active view repair Turn 没有 message: {turn_id}"
                        )
                    if turn_sequences.issubset(visible_sequences):
                        expected.append(
                            (
                                turn_id,
                                first_sequence,
                                user_sequence,
                                final_sequence,
                            )
                        )
                current = [
                    (
                        strict_text(row[0], field="context_view_turns.turn_id"),
                        strict_non_negative_int(
                            row[1], field="context_view_turns.logical_turn_ordinal"
                        ),
                        strict_optional_non_negative_int(
                            row[2], field="context_view_turns.user_message_sequence"
                        ),
                        strict_optional_non_negative_int(
                            row[3], field="context_view_turns.final_message_sequence"
                        ),
                    )
                    for row in connection.execute(
                        "SELECT turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence FROM context_view_turns WHERE view_id = ? ORDER BY logical_turn_ordinal",
                        (view_id,),
                    ).fetchall()
                ]
                normalized_expected = [
                    (turn_id, ordinal, user_sequence, final_sequence)
                    for ordinal, (
                        turn_id,
                        _first,
                        user_sequence,
                        final_sequence,
                    ) in enumerate(expected, start=1)
                ]
                view_header = connection.execute(
                    "SELECT head_turn_id, head_message_sequence, logical_turn_count FROM context_views WHERE view_id = ?",
                    (view_id,),
                ).fetchone()
                if view_header is None:
                    raise RuntimeError(f"active context view 不存在: {view_id}")
                stored_head_turn_id = strict_optional_text(
                    view_header[0], field="context_views.head_turn_id"
                )
                stored_head_sequence = strict_non_negative_int(
                    view_header[1], field="context_views.head_message_sequence"
                )
                stored_turn_count = strict_non_negative_int(
                    view_header[2], field="context_views.logical_turn_count"
                )
                expected_head = (
                    normalized_expected[-1][0] if normalized_expected else None
                )
                expected_sequence = max(visible_sequences, default=0)
                if (
                    current == normalized_expected
                    and stored_head_turn_id == expected_head
                    and stored_head_sequence == expected_sequence
                    and stored_turn_count == len(normalized_expected)
                ):
                    return False
                delete_result = connection.execute(
                    "DELETE FROM context_view_turns WHERE view_id = ?",
                    (view_id,),
                )
                if delete_result.rowcount != len(current):
                    raise RuntimeError(
                        f"active view repair 删除 Turn 行数不一致: {view_id}"
                    )
                insert_result = connection.executemany(
                    "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence) VALUES (?, ?, ?, ?, ?)",
                    (
                        (view_id, turn_id, ordinal, user_sequence, final_sequence)
                        for ordinal, (
                            turn_id,
                            _first,
                            user_sequence,
                            final_sequence,
                        ) in enumerate(expected, start=1)
                    ),
                )
                if insert_result.rowcount != len(normalized_expected):
                    raise RuntimeError(
                        f"active view repair 插入 Turn 行数不一致: {view_id}"
                    )
                header_result = connection.execute(
                    "UPDATE context_views SET head_turn_id = ?, head_message_sequence = ?, logical_turn_count = ? WHERE view_id = ?",
                    (
                        expected_head,
                        expected_sequence,
                        len(normalized_expected),
                        view_id,
                    ),
                )
                if header_result.rowcount != 1:
                    raise RuntimeError(f"active view repair header 更新失败: {view_id}")
                connection.commit()
                return True

    def ensure_active_view_contains_turn_root(
        self,
        thread_id: str,
        *,
        turn_id: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """把已接受 Turn root 幂等补入当前 active view 的 item 索引。

        acceptance 可能先于 LangGraph 创建初始化 view；因此 acceptance 事务
        本身没有可更新的 view。provider dispatch 前再次执行这个 owner-side
        同步，避免首个 assembly 在 active view 已建立后仍看不到 root。只更新
        SQLite view membership，不改变 canonical JSONL 或 view head。
        """
        thread_id = strict_text(thread_id, field="thread_id")
        turn_id = strict_text(turn_id, field="turn_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        if not thread_id or not turn_id:
            raise ValueError("ensure active view root 缺少 thread_id/turn_id")
        with self._host._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._host._connect(thread_id, checkpoint_ns) as connection:
                self._host._require_v2_runtime(connection)
                branch_row = connection.execute(
                    "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                    (checkpoint_ns,),
                ).fetchone()
                if branch_row is None:
                    raise RuntimeError(
                        f"rollout namespace 缺少 active branch: {checkpoint_ns!r}"
                    )
                active_branch_id = strict_text(
                    branch_row[0],
                    field="checkpoint_namespace_state.active_branch_id",
                )
                view_row = connection.execute(
                    "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
                    (active_branch_id,),
                ).fetchone()
                if view_row is None:
                    raise RuntimeError(
                        f"rollout active branch 不存在: {active_branch_id}"
                    )
                view_id = strict_optional_text(
                    view_row[0], field="branches.head_view_id"
                )
                if view_id is None:
                    return False
                root_row = connection.execute(
                    "SELECT root_input_item_id FROM turn_records WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if root_row is None:
                    raise KeyError(f"Turn root 不存在: {turn_id}")
                item_id = strict_text(
                    root_row[0], field=f"turn_records.root_input_item_id:{turn_id}"
                )
                if (
                    connection.execute(
                        "SELECT 1 FROM item_catalog WHERE item_id = ?",
                        (item_id,),
                    ).fetchone()
                    is None
                ):
                    raise RuntimeError(f"Turn root catalog 缺失: {item_id}")
                item_added = self._host._append_context_view_items(
                    connection,
                    checkpoint_ns=checkpoint_ns,
                    item_ids=(item_id,),
                )
                if not item_added:
                    return False
                view_turn = connection.execute(
                    "SELECT 1 FROM context_view_turns WHERE view_id = ? AND turn_id = ?",
                    (view_id, turn_id),
                ).fetchone()
                if view_turn is None:
                    ordinal_row = connection.execute(
                        "SELECT COALESCE(MAX(logical_turn_ordinal), 0) + 1 FROM context_view_turns WHERE view_id = ?",
                        (view_id,),
                    ).fetchone()
                    if ordinal_row is None:
                        raise RuntimeError(
                            f"active view Turn ordinal 无法读取: {view_id}"
                        )
                    logical_turn_ordinal = strict_non_negative_int(
                        ordinal_row[0],
                        field="context_view_turns.next_logical_turn_ordinal",
                    )
                    if logical_turn_ordinal == 0:
                        raise RuntimeError(
                            f"active view Turn ordinal 不能为 0: {view_id}"
                        )
                    turn_result = connection.execute(
                        "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence, root_input_item_id, fork_lineage_json) VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                        (
                            view_id,
                            turn_id,
                            logical_turn_ordinal,
                            item_id,
                            _turn_root_json({"source": "turn_root_reconciliation"}),
                        ),
                    )
                    if turn_result.rowcount != 1:
                        raise RuntimeError(
                            f"active view Turn root 写入失败: {view_id}/{turn_id}"
                        )
                    header_result = connection.execute(
                        "UPDATE context_views SET head_turn_id = ?, logical_turn_count = MAX(logical_turn_count, ?) WHERE view_id = ?",
                        (turn_id, logical_turn_ordinal, view_id),
                    )
                    if header_result.rowcount != 1:
                        raise RuntimeError(f"active view header 更新失败: {view_id}")
                connection.commit()
                return True


__all__ = ["RolloutMaintenanceOwner"]
