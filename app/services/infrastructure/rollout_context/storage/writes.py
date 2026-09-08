"""v2 checkpoint pending-write/finalization storage owner。

写入通过 RolloutStorage 的 v2 transaction 提交；这里不引入旧 message writer
或第二份 checkpoint 事实源。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

from langgraph.checkpoint.base import PendingWrite

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _pending_write_row(
    row: tuple[object, ...],
) -> tuple[str, str, int, str, str, bytes, int, str, str, str]:
    """严格解码 pending_writes，防止复制或读取时吞掉损坏行。"""
    if len(row) != 10:
        raise RuntimeError(f"pending_writes 行字段数非法: {len(row)}")
    (
        task_id,
        task_path,
        write_index,
        channel,
        serializer,
        value_blob,
        value_length,
        value_hash,
        status,
        created_at,
    ) = row
    task_id = strict_text(task_id, field="pending_writes.task_id")
    task_path = strict_text(task_path, field="pending_writes.task_path")
    write_index = strict_non_negative_int(
        write_index, field="pending_writes.write_index"
    )
    channel = strict_text(channel, field="pending_writes.channel")
    serializer = strict_text(serializer, field="pending_writes.serializer_name")
    if not isinstance(value_blob, (bytes, bytearray)):
        raise TypeError("pending_writes.value_blob 必须是 bytes")
    value_blob = bytes(value_blob)
    value_length = strict_non_negative_int(
        value_length, field="pending_writes.value_length"
    )
    if value_length != len(value_blob):
        raise RuntimeError(
            "pending_writes.value_length 与 value_blob 长度不一致: "
            f"task={task_id}, path={task_path}, index={write_index}"
        )
    value_hash = strict_text(value_hash, field="pending_writes.value_hash")
    if value_hash != sha256(value_blob).hexdigest():
        raise RuntimeError(
            "pending_writes.value_hash 与 value_blob 不一致: "
            f"task={task_id}, path={task_path}, index={write_index}"
        )
    status = strict_text(status, field="pending_writes.status")
    if status != "pending":
        raise RuntimeError(f"pending_writes.status 非法: {status}")
    created_at = strict_text(created_at, field="pending_writes.created_at")
    return (
        task_id,
        task_path,
        write_index,
        channel,
        serializer,
        value_blob,
        value_length,
        value_hash,
        status,
        created_at,
    )


def _assert_one_row(cursor: sqlite3.Cursor, *, context: str) -> None:
    if cursor.rowcount != 1:
        raise RuntimeError(f"{context} 影响行数异常: {cursor.rowcount}")


class RolloutWriteMixin:
    """pending write、checkpoint finalization 与删除 owner。"""

    def pending_writes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        *,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> list[PendingWrite]:
        strict_text(thread_id, field="pending_writes.thread_id")
        strict_text(
            checkpoint_ns, field="pending_writes.checkpoint_ns", allow_empty=True
        )
        strict_text(checkpoint_id, field="pending_writes.checkpoint_id")

        def read(connection: sqlite3.Connection) -> list[PendingWrite]:
            self._require_v2_runtime(connection)
            rows = connection.execute(
                "SELECT task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at FROM pending_writes WHERE checkpoint_id = ? AND status = 'pending' ORDER BY task_path, write_index",
                (checkpoint_id,),
            ).fetchall()
            result: list[PendingWrite] = []
            for raw_row in rows:
                (
                    task_id,
                    _task_path,
                    _write_index,
                    channel,
                    serializer,
                    blob,
                    _value_length,
                    _value_hash,
                    _status,
                    _created_at,
                ) = _pending_write_row(tuple(raw_row))
                result.append(
                    (task_id, channel, self.decode_value((serializer, bytes(blob))))
                )
            return result

        if snapshot is not None:
            self._require_v2_runtime(self._snapshot_connection(snapshot))
            return read(self._snapshot_connection(snapshot))
        with self._connect(thread_id, checkpoint_ns) as connection:
            self._require_v2_runtime(connection)
            return read(connection)

    def copy_pending_writes(
        self,
        *,
        source_thread_id: str,
        source_checkpoint_id: str,
        target_thread_id: str,
        target_checkpoint_id: str,
        checkpoint_ns: str = "",
    ) -> None:
        from app.services.infrastructure.rollout_context.storage.primitives import (
            _RolloutFileLock,
        )

        """将源 checkpoint 的完整 pending_writes 行复制到子 rollout。"""
        source_thread_id = strict_text(
            source_thread_id, field="copy_pending_writes.source_thread_id"
        )
        source_checkpoint_id = strict_text(
            source_checkpoint_id, field="copy_pending_writes.source_checkpoint_id"
        )
        target_thread_id = strict_text(
            target_thread_id, field="copy_pending_writes.target_thread_id"
        )
        target_checkpoint_id = strict_text(
            target_checkpoint_id, field="copy_pending_writes.target_checkpoint_id"
        )
        strict_text(
            checkpoint_ns,
            field="copy_pending_writes.checkpoint_ns",
            allow_empty=True,
        )
        source_root = self.root(source_thread_id, checkpoint_ns)
        if not source_root.is_dir():
            raise KeyError(source_thread_id)
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(source_thread_id, checkpoint_ns) as connection:
                raw_rows = connection.execute(
                    "SELECT task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at FROM pending_writes WHERE checkpoint_id = ? ORDER BY task_path, write_index",
                    (source_checkpoint_id,),
                ).fetchall()
                rows = [_pending_write_row(tuple(row)) for row in raw_rows]
        finally:
            source_lock.release()
        if not rows:
            return
        with self._lock(target_thread_id, checkpoint_ns):
            self.initialize(target_thread_id, checkpoint_ns)
            with self._connect(target_thread_id, checkpoint_ns) as connection:
                checkpoint_exists = connection.execute(
                    "SELECT 1 FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                    (target_checkpoint_id, checkpoint_ns),
                ).fetchone()
                if checkpoint_exists is None:
                    raise KeyError(target_checkpoint_id)
                for row in rows:
                    (
                        task_id,
                        task_path,
                        write_index,
                        channel,
                        serializer_name,
                        value_blob,
                        value_length,
                        value_hash,
                        status,
                        created_at,
                    ) = row
                    existing = connection.execute(
                        "SELECT task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at FROM pending_writes WHERE checkpoint_id = ? AND task_id = ? AND task_path = ? AND write_index = ?",
                        (target_checkpoint_id, task_id, task_path, write_index),
                    ).fetchone()
                    if existing is not None:
                        if _pending_write_row(tuple(existing)) != row:
                            raise RuntimeError(
                                "target pending_writes 已存在不同内容: "
                                f"task={task_id}, path={task_path}, index={write_index}"
                            )
                        continue
                    cursor = connection.execute(
                        "INSERT INTO pending_writes(checkpoint_id, task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            target_checkpoint_id,
                            task_id,
                            task_path,
                            write_index,
                            channel,
                            serializer_name,
                            value_blob,
                            value_length,
                            value_hash,
                            status,
                            created_at,
                        ),
                    )
                    _assert_one_row(cursor, context="复制 pending_writes")

    def append_writes(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        writes: Iterable[PendingWrite],
        task_id: str,
        task_path: str,
    ) -> None:
        strict_text(thread_id, field="append_writes.thread_id")
        strict_text(
            checkpoint_ns,
            field="append_writes.checkpoint_ns",
            allow_empty=True,
        )
        strict_text(checkpoint_id, field="append_writes.checkpoint_id")
        strict_text(task_id, field="append_writes.task_id")
        strict_text(task_path, field="append_writes.task_path")
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                for index, (channel, value) in enumerate(writes):
                    serializer, blob, length, digest = self._encode(value)
                    channel = strict_text(channel, field="pending_writes.channel")
                    expected = _pending_write_row(
                        (
                            task_id,
                            task_path,
                            index,
                            channel,
                            strict_text(
                                serializer, field="pending_writes.serializer_name"
                            ),
                            bytes(blob),
                            strict_non_negative_int(
                                length, field="pending_writes.value_length"
                            ),
                            strict_text(digest, field="pending_writes.value_hash"),
                            "pending",
                            _now(),
                        )
                    )
                    existing = connection.execute(
                        "SELECT task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at FROM pending_writes WHERE checkpoint_id = ? AND task_id = ? AND task_path = ? AND write_index = ?",
                        (checkpoint_id, task_id, task_path, index),
                    ).fetchone()
                    if existing is not None:
                        stored = _pending_write_row(tuple(existing))
                        if stored[:9] != expected[:9]:
                            raise RuntimeError(
                                "pending_writes 重试内容不一致: "
                                f"task={task_id}, path={task_path}, index={index}"
                            )
                        continue
                    cursor = connection.execute(
                        "INSERT INTO pending_writes(checkpoint_id, task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (
                            checkpoint_id,
                            *expected[:8],
                            expected[9],
                        ),
                    )
                    _assert_one_row(cursor, context="追加 pending_writes")

    def copy_turn_finalizations(
        self,
        *,
        source_thread_id: str,
        source_checkpoint_id: str | None,
        target_thread_id: str,
        checkpoint_ns: str = "",
    ) -> int:
        from app.services.infrastructure.rollout_context.storage.primitives import (
            _RolloutFileLock,
        )

        """把源 checkpoint 中已完成 Turn 的 SQLite 指针复制到目标 rollout。

        fork 通过 ``put`` 重新追加消息时，消息正文可以独立复制，但
        ``final_message_id``、``final_message_sequence`` 和 Turn 终态并不属于
        LangGraph checkpoint 的 channel value。如果不显式复制，目标 rollout
        会把这些本来已经完成的历史 Turn 当成 running，前端就会永久显示
        “正在处理”。这里按源 view 的 Turn membership 选择完成指针，再用
        不可变 message_id 在目标 rollout 中定位对应消息；找不到的消息跳过，
        以支持按 Turn 截断的 context fork。
        """
        strict_text(source_thread_id, field="copy_turn_finalizations.source_thread_id")
        strict_optional_text(
            source_checkpoint_id,
            field="copy_turn_finalizations.source_checkpoint_id",
        )
        strict_text(target_thread_id, field="copy_turn_finalizations.target_thread_id")
        strict_text(
            checkpoint_ns,
            field="copy_turn_finalizations.checkpoint_ns",
            allow_empty=True,
        )
        source_root = self.root(source_thread_id, checkpoint_ns)
        if not source_root.is_dir():
            raise KeyError(source_thread_id)
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(source_thread_id, checkpoint_ns) as source_connection:
                # 该路径只复制已提交的 v2 派生 final pointer；v1 message-line
                # 必须先经显式 legacy_import_v1_to_v2 staging，不能由 fork
                # 正常运行时偷偷重新打开旧事实源。
                self._require_v2_runtime(source_connection)
                if source_checkpoint_id is not None:
                    source_view_row = source_connection.execute(
                        "SELECT view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
                        (source_checkpoint_id, checkpoint_ns),
                    ).fetchone()
                    if source_view_row is None:
                        raise KeyError(source_checkpoint_id)
                    source_view_id = strict_text(
                        source_view_row[0],
                        field="copy_turn_finalizations.source_view_id",
                    )
                else:
                    source_view_row = source_connection.execute(
                        "SELECT head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
                        (checkpoint_ns,),
                    ).fetchone()
                    source_view_id = (
                        strict_text(
                            source_view_row[0],
                            field="copy_turn_finalizations.source_view_id",
                        )
                        if source_view_row is not None
                        and source_view_row[0] is not None
                        else None
                    )
                if source_view_id is None:
                    return 0
                source_rows = [
                    (
                        strict_text(
                            turn_id,
                            field="copy_turn_finalizations.source_turn_id",
                        ),
                        strict_text(
                            final_message_id,
                            field="copy_turn_finalizations.source_final_message_id",
                        ),
                    )
                    for turn_id, final_message_id in source_connection.execute(
                        """
                    SELECT DISTINCT t.turn_id, t.final_message_id
                    FROM context_view_turns AS cvt
                    JOIN turns AS t ON t.turn_id = cvt.turn_id
                    WHERE cvt.view_id = ?
                      AND t.status IN ('completed', 'succeeded')
                      AND t.final_message_id IS NOT NULL
                    ORDER BY t.turn_ordinal
                    """,
                        (source_view_id,),
                    ).fetchall()
                ]
        finally:
            source_lock.release()

        if not source_rows:
            return 0

        with self._lock(target_thread_id, checkpoint_ns):
            self.initialize(target_thread_id, checkpoint_ns)
            with self._connect(target_thread_id, checkpoint_ns) as target_connection:
                self._require_v2_runtime(target_connection)
                timestamp = _now()
                transaction_id = uuid4().hex
                active_branch_row = target_connection.execute(
                    "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                    (checkpoint_ns,),
                ).fetchone()
                active_branch_id = (
                    strict_text(
                        active_branch_row[0],
                        field="copy_turn_finalizations.active_branch_id",
                    )
                    if active_branch_row is not None
                    and active_branch_row[0] is not None
                    else None
                )
                copied = 0
                for source_turn_id, source_final_message_id in source_rows:
                    target_message = target_connection.execute(
                        "SELECT message_sequence, turn_id FROM messages WHERE message_id = ?",
                        (source_final_message_id,),
                    ).fetchone()
                    if target_message is None:
                        continue
                    target_message_turn_id = strict_text(
                        target_message[1],
                        field="copy_turn_finalizations.target_message.turn_id",
                    )
                    if target_message_turn_id != source_turn_id:
                        raise RuntimeError(
                            "target final message 的 Turn identity 与 source 不一致: "
                            f"message={source_final_message_id}"
                        )
                    target_turn = target_connection.execute(
                        "SELECT turn_id FROM turns WHERE turn_id = ?",
                        (source_turn_id,),
                    ).fetchone()
                    if target_turn is None:
                        continue
                    strict_text(
                        target_turn[0],
                        field="copy_turn_finalizations.target_turn_id",
                    )
                    target_sequence = strict_non_negative_int(
                        target_message[0],
                        field="copy_turn_finalizations.target_message_sequence",
                    )
                    if target_sequence <= 0:
                        raise RuntimeError(
                            "copy_turn_finalizations.target_message_sequence 必须为正数"
                        )
                    cursor = target_connection.execute(
                        "UPDATE turns SET final_message_sequence = ?, final_message_id = ?, status = 'completed', updated_at = ? WHERE turn_id = ?",
                        (
                            target_sequence,
                            source_final_message_id,
                            timestamp,
                            source_turn_id,
                        ),
                    )
                    _assert_one_row(cursor, context="复制 Turn final 指针")
                    context_view_turn = target_connection.execute(
                        "SELECT 1 FROM context_view_turns WHERE turn_id = ? LIMIT 1",
                        (source_turn_id,),
                    ).fetchone()
                    if context_view_turn is not None:
                        cursor = target_connection.execute(
                            "UPDATE context_view_turns SET final_message_sequence = ? WHERE turn_id = ?",
                            (target_sequence, source_turn_id),
                        )
                        _assert_one_row(
                            cursor, context="复制 context view Turn final 指针"
                        )
                    cursor = target_connection.execute(
                        "UPDATE message_projections SET phase = 'final_answer', updated_at = ? WHERE message_sequence = ?",
                        (timestamp, target_sequence),
                    )
                    _assert_one_row(cursor, context="复制 final message projection")
                    self._insert_control(
                        target_connection,
                        "checkpoint_finalized",
                        "turn",
                        source_turn_id,
                        active_branch_id,
                        None,
                        None,
                        {
                            "final_message_sequence": target_sequence,
                            "copied_from_session_id": source_thread_id,
                            "copied_from_message_id": source_final_message_id,
                        },
                        transaction_id,
                        timestamp,
                    )
                    copied += 1
                if copied:
                    last_control = target_connection.execute(
                        "SELECT control_sequence FROM control_events WHERE transaction_id = ? ORDER BY control_sequence DESC LIMIT 1",
                        (transaction_id,),
                    ).fetchone()
                    if last_control is None:
                        raise RuntimeError(
                            "复制 Turn finalization 未生成 control event"
                        )
                    last_control_sequence = strict_non_negative_int(
                        last_control[0],
                        field="copy_turn_finalizations.last_control_sequence",
                    )
                    cursor = target_connection.execute(
                        "UPDATE database_meta SET last_control_sequence = ?, updated_at = ? WHERE singleton_id = 1",
                        (
                            last_control_sequence,
                            timestamp,
                        ),
                    )
                    _assert_one_row(
                        cursor, context="复制 Turn finalization database_meta"
                    )
                target_connection.commit()
                return copied

    def delete_thread(self, thread_id: str) -> None:
        rollout = self.root(thread_id)
        self._active_fork_materializations.discard((thread_id, ""))
        if not rollout.exists():
            return
        self.release_fork_retentions(thread_id)
        with self._lock(thread_id, ""):
            if rollout.exists():
                shutil.rmtree(rollout)
