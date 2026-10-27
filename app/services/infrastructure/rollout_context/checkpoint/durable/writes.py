"""v2 checkpoint pending-write/finalization storage owner。

写入通过 RolloutContextStore 的 v2 transaction 提交；这里不引入旧 message writer
或第二份 checkpoint 事实源。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from hashlib import sha256
from typing import TYPE_CHECKING

from langgraph.checkpoint.base import WRITES_IDX_MAP, PendingWrite

from app.core.sqlite_state import utc_now_text as _now
from app.services.infrastructure.rollout_context.checkpoint.codec.pending_messages import (
    MESSAGE_REFERENCE_SERIALIZER,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.message_writes import (
    PendingMessageReferences,
    original_write_fingerprint,
)
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
    channel = strict_text(channel, field="pending_writes.channel")
    if not isinstance(write_index, int) or isinstance(write_index, bool):
        raise TypeError("pending_writes.write_index 必须是整数")
    if write_index < 0 and WRITES_IDX_MAP.get(channel) != write_index:
        raise ValueError("pending_writes 负索引必须匹配 LangGraph 控制通道")
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
                "SELECT task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at FROM pending_writes WHERE checkpoint_id = ? AND status = 'pending'" + channel_filter + " ORDER BY task_path, task_id, write_index",
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
                value = (
                    PendingMessageReferences(
                        self._storage, connection, session_id=session_id,
                        thread_id=thread_id, checkpoint_ns=checkpoint_ns,
                    ).restore(serializer, blob)
                    if serializer.startswith(MESSAGE_REFERENCE_SERIALIZER)
                    else self._storage.decode_value((serializer, blob))
                )
                result.append((task_id, channel, value))
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
        writes: Iterable[tuple[str, object]],
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
                    # 官方控制槽可更新，且不能与恢复后节点的普通第零项互相占用。
                    # 普通业务写入仍保持原有的不可变重试核对。
                    index = WRITES_IDX_MAP.get(channel, index)
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
                        if index < 0:
                            if stored[:4] != expected[:4]:
                                raise RuntimeError("pending_writes 控制槽身份冲突")
                            cursor = connection.execute(
                                "UPDATE pending_writes SET serializer_name=?, value_blob=?, value_length=?, value_hash=? "
                                "WHERE checkpoint_id=? AND task_id=? AND task_path=? AND write_index=?",
                                (*expected[4:8], checkpoint_id, task_id, task_path, index),
                            )
                            _assert_one_row(cursor, context="更新 pending_writes 控制槽")
                            continue
                        if (stored[:4] != expected[:4] or stored[8] != expected[8]
                                or original_write_fingerprint(*stored[4:8], storage=self._storage) != original_write_fingerprint(*expected[4:8], storage=self._storage)):
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
