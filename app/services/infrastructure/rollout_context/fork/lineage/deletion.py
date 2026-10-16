"""v2 checkpoint pending-write/finalization storage owner。

写入通过 RolloutContextStore 的 v2 transaction 提交；这里不引入旧 message writer
或第二份 checkpoint 事实源。
"""

from __future__ import annotations

import shutil
import sqlite3
from hashlib import sha256
from typing import TYPE_CHECKING

from app.core.session_lifecycle_gate import SessionLifecycleGate
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.attachment_blob_catalog.store import (
        AttachmentBlobStore,
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



class RolloutForkDeletionMixin:
    async def delete_thread(self, session_id: str, *, thread_id: str, attachment_store: AttachmentBlobStore | None) -> None:
        await self.release_fork_retentions(session_id, target_thread_id=thread_id)
        self._storage._active_fork_materializations.difference_update(
            key
            for key in tuple(self._storage._active_fork_materializations)
            if key[:2] == (session_id, thread_id)
        )
        async with SessionLifecycleGate(self._storage.sessions_dir).exclusive(session_id):
            if attachment_store is not None:
                attachment_store.release_thread_references(session_id, thread_id)
            rollout = self._storage.root(session_id, thread_id=thread_id)
            with self._storage._lock(thread_id, "", session_id=session_id):
                if rollout.exists():
                    shutil.rmtree(rollout)
