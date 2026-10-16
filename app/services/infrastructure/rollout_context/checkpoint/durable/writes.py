"""v2 checkpoint pending-write/finalization storage owner。

写入通过 RolloutContextStore 的 v2 transaction 提交；这里不引入旧 message writer
或第二份 checkpoint 事实源。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from hashlib import sha256
from typing import TYPE_CHECKING

from langgraph.checkpoint.base import PendingWrite

from app.core.sqlite_state import utc_now_text as _now
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )


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
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        *,
        snapshot: RolloutReadSnapshot | None = None,
        requested_channels: frozenset[str] | None = None,
    ) -> list[PendingWrite]:
        strict_text(session_id, field="pending_writes.session_id")
        strict_text(thread_id, field="pending_writes.thread_id")
        strict_text(
            checkpoint_ns, field="pending_writes.checkpoint_ns", allow_empty=True
        )
        strict_text(checkpoint_id, field="pending_writes.checkpoint_id")

        def read(connection: sqlite3.Connection) -> list[PendingWrite]:
            self._storage._require_v2_runtime(connection)
            channel_filter = ""
            parameters: tuple[object, ...] = (checkpoint_id,)
            if requested_channels is not None:
                if not requested_channels:
                    return []
                selected = sorted(requested_channels)
                channel_filter = f" AND channel IN ({','.join('?' for _ in selected)})"
                parameters += tuple(selected)
            rows = connection.execute(
                "SELECT task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at FROM pending_writes WHERE checkpoint_id = ? AND status = 'pending'" + channel_filter + " ORDER BY task_path, write_index",
                parameters,
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
                    (task_id, channel, self._storage.decode_value((serializer, bytes(blob))))
                )
            return result

        with self._storage.read_connection(
            session_id, thread_id, checkpoint_ns, snapshot=snapshot,
        ) as connection:
            return read(connection)


    def append_writes(
        self,
        *,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        writes: Iterable[PendingWrite],
        task_id: str,
        task_path: str,
    ) -> None:
        strict_text(session_id, field="append_writes.session_id")
        strict_text(thread_id, field="append_writes.thread_id")
        strict_text(
            checkpoint_ns,
            field="append_writes.checkpoint_ns",
            allow_empty=True,
        )
        strict_text(checkpoint_id, field="append_writes.checkpoint_id")
        strict_text(task_id, field="append_writes.task_id")
        strict_text(task_path, field="append_writes.task_path")
        with self._storage._lock(thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            with self._storage._connect(
                thread_id, checkpoint_ns, session_id=session_id
            ) as connection:
                for index, (channel, value) in enumerate(writes):
                    serializer, blob, length, digest = self._storage._encode(value)
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
