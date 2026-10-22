"""rollout checkpoint index 的读取快照与维护。"""

from __future__ import annotations

import sqlite3
from collections.abc import (
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import contextmanager

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    current_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutCheckpointIndex,
    RolloutManifest,
    RolloutNotMaterializedError,
    RolloutReadSnapshot,
    _RolloutFileLock,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    V2ItemCommitCoordinator,
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

__all__ = ["RolloutIndexMixin"]


class RolloutIndexMixin:
    def read_lock(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> _RolloutFileLock:
        """读取只借用既有锁文件，不物化缺失 rollout。"""
        root = self._host.root(session_id, checkpoint_ns, thread_id=thread_id)
        lock_path = root.parent / ".rollout.write.lock"
        if (
            not root.exists() and not root.is_symlink()
            and not lock_path.exists() and not lock_path.is_symlink()
        ):
            raise RolloutNotMaterializedError(
                f"rollout 尚未物化: session_id={session_id}, thread_id={thread_id}"
            )
        if not lock_path.is_file() or lock_path.is_symlink():
            raise RuntimeError(
                "recovery_required: rollout 缺少既有普通写锁文件: "
                f"session_id={session_id}, thread_id={thread_id}"
            )
        return _RolloutFileLock(lock_path, exclusive=False)

    def open_read_snapshot(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str | None = "",
        *,
        validate_integrity: bool = False,
    ) -> RolloutReadSnapshot:
        """打开只读事务；未指定 namespace 时从同库选择已发布的 manifest。"""
        if checkpoint_ns is not None and not isinstance(checkpoint_ns, str):
            raise TypeError("rollout checkpoint_ns 必须是字符串或 null")
        root = self._host.root(session_id, checkpoint_ns or "", thread_id=thread_id)
        jsonl_path = root / "rollout.jsonl"
        index_path = root / "index.sqlite"
        file_lock = self.read_lock(session_id, thread_id, checkpoint_ns or "")
        file_lock.acquire()
        connection: sqlite3.Connection | None = None
        try:
            if (
                not root.is_dir() or root.is_symlink()
                or not jsonl_path.is_file() or jsonl_path.is_symlink()
                or not index_path.is_file() or index_path.is_symlink()
            ):
                raise RuntimeError(
                    "recovery_required: rollout 权威文件缺失或路径非法；禁止读取时重建: "
                    f"session_id={session_id}, thread_id={thread_id}"
                )
            connection = self._host._connect(
                thread_id,
                checkpoint_ns or "",
                session_id=session_id,
                read_only=True,
            )
            connection.execute("BEGIN")
            self._host._require_v2_runtime(connection)
            self._validate_schema_state(connection)
            self._host._validate_reasoning_projection_connection(connection)
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
            actual_owner = current_scope(connection)
            expected_owner = (session_id, thread_id)
            if actual_owner != expected_owner:
                raise ValueError(
                    "source-mismatch: 读取方与目标 rollout catalog owner 不一致; "
                    f"expected={expected_owner!r}, actual={actual_owner!r}"
                )
            self._validate_v2_commit_offsets(
                connection, jsonl_path, validate_jsonl_items=validate_integrity
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
                raise RuntimeError("recovery_required: rollout.jsonl 小于 SQLite 已提交偏移")
            integrity = (
                connection.execute("PRAGMA integrity_check").fetchone()[0]
                if validate_integrity
                else "ok"
            )
            if integrity != "ok":
                raise RuntimeError(
                    "recovery_required: rollout SQLite integrity_check 失败: "
                    f"session_id={session_id}, thread_id={thread_id}: {integrity}"
                )
            if checkpoint_ns is None:
                namespace = connection.execute(
                    "SELECT checkpoint_ns FROM checkpoint_namespace_state ORDER BY checkpoint_ns LIMIT 1"
                ).fetchone()
                if namespace is None:
                    raise RuntimeError("rollout 缺少已发布的 checkpoint namespace 状态")
                checkpoint_ns = strict_text(namespace[0], field="checkpoint_namespace_state.checkpoint_ns", allow_empty=True)
            manifest = self._manifest_from_connection(connection, checkpoint_ns)
        except sqlite3.DatabaseError as error:
            if connection is not None:
                connection.close()
            file_lock.release()
            raise RuntimeError(
                "recovery_required: rollout SQLite 无法读取；"
                "必须从已验证的 SQLite backup 执行显式恢复，禁止从 JSONL 重建: "
                f"session_id={session_id}, thread_id={thread_id}"
            ) from error
        except Exception:
            if connection is not None:
                connection.close()
            file_lock.release()
            raise
        return RolloutReadSnapshot(
            session_id,
            thread_id,
            checkpoint_ns,
            manifest,
            connection,
            file_lock,
        )

    def validate_index(
        self, session_id: str, thread_id: str, checkpoint_ns: str = ""
    ) -> RolloutReadSnapshot:
        """执行完整 SQLite integrity_check。"""
        return self.open_read_snapshot(
            session_id,
            thread_id,
            checkpoint_ns,
            validate_integrity=True,
        )

    def repair_index(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        session_id: str,
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
        session_id: str,
        commit_kind: str,
        commit_mode: str | None = None,
        outcome: str | None = None,
        metadata: Mapping[str, object] | None = None,
        subject_id: str | None = None,
        idempotency_key: str | None = None,
        begin_transaction: bool = True,
        transaction_id: str | None = None,
        timestamp: str | None = None,
        first_message_sequence: int | None = None,
        last_message_sequence: int | None = None,
    ) -> tuple[int, int]:
        """通过统一 coordinator 执行 v2 item-bearing 原子提交。"""
        coordinator = V2ItemCommitCoordinator(
            jsonl_path=self._host.jsonl_path(
                session_id,
                checkpoint_ns,
                thread_id=thread_id,
            ),
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
            transaction_id=transaction_id,
            timestamp=timestamp,
            first_message_sequence=first_message_sequence,
            last_message_sequence=last_message_sequence,
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

    def rollout_id(
        self, thread_id: str, checkpoint_ns: str = "", *, session_id: str
    ) -> str:
        with self.open_read_snapshot(session_id, thread_id, checkpoint_ns) as snapshot:
            return snapshot.rollout_id

    @contextmanager
    def _existing_index_connection(
        self,
        thread_id: str,
        checkpoint_ns: str,
        *,
        session_id: str | None = None,
    ) -> Iterator[sqlite3.Connection]:
        """读取既有 authority；完全损坏时只报告恢复要求，不创建新库。"""
        try:
            with self._host._connect(
                thread_id,
                checkpoint_ns,
                session_id=session_id,
                read_only=True,
            ) as connection:
                yield connection
        except sqlite3.DatabaseError as error:
            raise RuntimeError(
                "recovery_required: rollout SQLite 无法读取；"
                "必须从已验证的 SQLite backup 执行显式恢复，禁止从 JSONL 重建: "
                f"session_id={session_id}, thread_id={thread_id}"
            ) from error
