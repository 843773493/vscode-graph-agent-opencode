"""rollout checkpoint index 的读取快照与维护。"""

from __future__ import annotations

import sqlite3
from collections.abc import (
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import contextmanager

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutCheckpointIndex,
    RolloutManifest,
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
