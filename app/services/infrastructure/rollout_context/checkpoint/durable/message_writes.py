"""pending 消息在同一发布事务内从暂存正文转为精确 canonical 引用。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.messages import BaseMessage, message_to_dict

from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    canonical_message_anchor,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.pending_messages import (
    MESSAGE_REFERENCE_SERIALIZER,
    reference_message_write,
    restore_message_write,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    find_message_locator,
    read_message_group,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )


class PendingMessageReferences:
    """只读本地 canonical；引用保留历史 owner，不按消息 ID 猜测最新版本。"""

    def __init__(self, storage: RolloutStorage, connection: sqlite3.Connection, *, session_id: str, thread_id: str, checkpoint_ns: str, jsonl_path: Path | None = None):
        self.storage = storage
        self.connection = connection
        self.owner = (session_id, thread_id)
        self.checkpoint_ns = checkpoint_ns
        self.jsonl_path = jsonl_path or storage.jsonl_path(session_id, checkpoint_ns, thread_id=thread_id)

    def _canonical(self, item_id: str) -> tuple[dict[str, object], Mapping[str, object]]:
        row = self.connection.execute(
            "SELECT item.jsonl_offset,item.jsonl_length,execution.session_id,execution.thread_id,message.message_id "
            "FROM item_catalog item JOIN executions execution ON execution.execution_id=item.execution_id "
            "JOIN messages message ON message.jsonl_offset=item.jsonl_offset AND message.jsonl_length=item.jsonl_length "
            "WHERE item.item_id=?", (item_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"pending message canonical 引用缺失: {item_id}")
        require_historical_scope(self.connection, row[2], row[3], current_owner=self.owner)
        with self.jsonl_path.open("rb") as stream:
            group = read_message_group(
                self.connection, stream, offset=row[0], length=row[1], read_item=self.storage._read_v2_item_at,
            )
        reference = {
            "session_id": row[2], "thread_id": row[3], "checkpoint_ns": self.checkpoint_ns,
            "item_id": item_id, "message_id": row[4],
            "group": [[item.item_id, item.content_hash] for item in group],
        }
        return reference, self.storage._codec().project_message(group)

    def locate(self, message: BaseMessage, *, item_ids: Mapping[str, str] | None = None) -> tuple[dict[str, object], Mapping[str, object]] | None:
        if item_ids is not None and message.id in item_ids:
            return self._canonical(item_ids[message.id])
        anchor = canonical_message_anchor(message_to_dict(message))
        if anchor is None and self.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id=?", (message.id,),
        ).fetchone()[0] > 1:
            # 原写入暂存到其 checkpoint 的正式发布事务取得版本坐标；不猜正文或 latest。
            return None
        locator = find_message_locator(
            self.connection, message.id, item_id=anchor,
        )
        if locator is None:
            return None
        row = self.connection.execute(
            "SELECT item_id FROM item_catalog WHERE jsonl_offset=? AND jsonl_length=?", locator[3:5],
        ).fetchone()
        return self._canonical(row[0])

    def read(self, reference: Mapping[str, object]) -> Mapping[str, object]:
        if not isinstance(reference.get("item_id"), str):
            raise TypeError("pending message canonical 引用缺少 item_id")
        actual, message = self._canonical(reference["item_id"])
        if reference != actual:
            raise ValueError(f"pending message canonical owner/namespace/version/hash 不一致: {reference['item_id']}")
        return message

    def restore(self, serializer: str, blob: bytes) -> object:
        value = decode_message_reference(self.storage, serializer, blob)
        return restore_message_write(value["write"], self.read)


def publish_message_write_references(
    references: PendingMessageReferences, *,
    validate_row: Callable[[tuple[object, ...]], tuple[str, str, int, str, str, bytes, int, str, str, str]],
    parent_checkpoint_id: str | None = None,
    item_ids: Mapping[str, str] | None = None,
) -> None:
    """JSONL/catalog 已在当前事务准备完毕；失败时引用与正文共同回滚。"""
    connection = references.connection
    if not connection.in_transaction:
        raise RuntimeError("pending message 引用发布必须属于 checkpoint 原事务")
    rows = connection.execute(
        "SELECT checkpoint_id,task_id,task_path,write_index,channel,serializer_name,value_blob,"
        "value_length,value_hash,status,created_at FROM pending_writes "
        "WHERE channel='messages' AND substr(serializer_name,1,?)<>?",
        (len(MESSAGE_REFERENCE_SERIALIZER), MESSAGE_REFERENCE_SERIALIZER),
    ).fetchall()
    for row in rows:
        validated = validate_row(tuple(row[1:]))
        serializer, blob, length, digest = validated[4:8]
        value = references.storage.decode_value((serializer, blob))
        selected_ids = item_ids if row[0] == parent_checkpoint_id else None
        descriptor = reference_message_write(value, lambda message, selected_ids=selected_ids: references.locate(
            message, item_ids=selected_ids,
        ))
        if descriptor is None:
            # 未发布或不同版本仍是同一 pending row 的暂存态；不能丢掉崩溃恢复所需原写入。
            continue
        encoded_serializer, encoded, encoded_length, encoded_hash = references.storage._encode({
            "original": {"serializer": serializer, "length": length, "hash": digest},
            "write": descriptor,
        })
        connection.execute(
            "UPDATE pending_writes SET serializer_name=?,value_blob=?,value_length=?,value_hash=? "
            "WHERE checkpoint_id=? AND task_id=? AND task_path=? AND write_index=?",
            (MESSAGE_REFERENCE_SERIALIZER + encoded_serializer, encoded, encoded_length, encoded_hash, *row[:4]),
        )


def decode_message_reference(storage: RolloutStorage, serializer: str, blob: bytes) -> dict[str, object]:
    if not serializer.startswith(MESSAGE_REFERENCE_SERIALIZER) or not serializer[len(MESSAGE_REFERENCE_SERIALIZER):]:
        raise ValueError("pending message 引用 serializer 非法")
    value = storage.decode_value((serializer[len(MESSAGE_REFERENCE_SERIALIZER):], blob))
    if not isinstance(value, dict) or set(value) != {"original", "write"} or not isinstance(value["write"], dict):
        raise ValueError("pending message 引用 envelope 非法")
    original = value["original"]
    if (not isinstance(original, dict) or set(original) != {"serializer", "length", "hash"}
            or not isinstance(original["serializer"], str) or not original["serializer"]
            or original["serializer"].startswith(MESSAGE_REFERENCE_SERIALIZER)
            or type(original["length"]) is not int or original["length"] < 0
            or not isinstance(original["hash"], str) or len(original["hash"]) != sha256().digest_size * 2
            or any(character not in "0123456789abcdef" for character in original["hash"])):
        raise ValueError("pending message 原写入 fingerprint 非法")
    return value


def original_write_fingerprint(serializer: str, blob: bytes, length: int, digest: str, *, storage: RolloutStorage) -> tuple[str, int, str]:
    if not serializer.startswith(MESSAGE_REFERENCE_SERIALIZER):
        return serializer, length, digest
    original = decode_message_reference(storage, serializer, blob)["original"]
    return original["serializer"], original["length"], original["hash"]
