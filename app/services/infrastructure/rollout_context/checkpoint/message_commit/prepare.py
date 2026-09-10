"""准备消息和 canonical group，写入动作仍由同一 checkpoint 事务完成。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    SEMANTIC_FIELDS,
    semantic_metadata,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_message_group,
)
from app.services.infrastructure.rollout_context.storage.primitives import MessageCodec
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
    canonical_json_text,
)

ItemLocation = tuple[CanonicalItemRecord, int, int]
PreparedMessage = tuple[
    int,
    str,
    str,
    str,
    str,
    int,
    int,
    bytes,
    str,
    str,
    tuple[ItemLocation, ...],
]


@dataclass
class MessageBatch:
    next_item_sequence: int
    last_message_sequence: int
    first_message_sequence: int | None = None
    prepared: list[PreparedMessage] = field(default_factory=list)
    visible_sequences: list[int] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)


def _compare_group(
    expected: Sequence[CanonicalItemRecord],
    actual: Sequence[CanonicalItemRecord],
) -> None:
    if len(expected) != len(actual):
        raise ValueError("canonical message group 成员数发生变化")
    for candidate, stored in zip(expected, actual, strict=True):
        if any(
            getattr(candidate, name) != getattr(stored, name)
            for name in (
                "item_id",
                "semantic_kind",
                "payload_kind",
                "content_hash",
                "turn_id",
                "turn_scope",
            )
        ):
            raise ValueError(
                f"canonical item 与 message projection 内容不一致: {stored.item_id}"
            )
        candidate_semantic_metadata = semantic_metadata(candidate.metadata)
        stored_semantic_metadata = semantic_metadata(stored.metadata)
        for name in SEMANTIC_FIELDS:
            if candidate_semantic_metadata.get(name) != stored_semantic_metadata.get(
                name
            ):
                raise ValueError(
                    f"canonical item semantic/provenance metadata 不一致: {stored.item_id}:{name}"
                )
        for name in ("projection_group", "reasoning_carrier"):
            if candidate.metadata.get(name) != stored.metadata.get(name):
                raise ValueError(
                    f"canonical item semantic/provenance metadata 不一致: {stored.item_id}:{name}"
                )


def prepare_messages(
    *,
    connection: sqlite3.Connection,
    jsonl: Path,
    codec: MessageCodec,
    read_item: Callable[..., CanonicalItemRecord],
    messages: Sequence[object],
    last_message_sequence: int,
    next_item_sequence: int,
    offset: int,
) -> MessageBatch:
    batch = MessageBatch(next_item_sequence, last_message_sequence)
    identities = [
        codec.message_id(message, index) for index, message in enumerate(messages)
    ]
    if len(set(identities)) != len(identities):
        raise ValueError("一个 checkpoint 不能重复引用同一个 canonical message_id")
    current_turn: str | None = None
    with jsonl.open("rb") as stream:
        for message_id, message in zip(identities, messages, strict=True):
            indexed = connection.execute(
                "SELECT message_sequence, turn_id FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            catalog = connection.execute(
                "SELECT jsonl_offset, jsonl_length, turn_id FROM item_catalog WHERE item_id = ?",
                (f"item-{message_id}",),
            ).fetchone()
            if indexed is not None and catalog is None:
                raise RuntimeError(
                    f"v2 message projection 缺少 canonical item: {message_id}"
                )
            stored = (
                read_message_group(
                    connection,
                    stream,
                    offset=catalog[0],
                    length=catalog[1],
                    read_item=read_item,
                )
                if catalog is not None
                else ()
            )
            turn_id = (
                indexed[1]
                if indexed is not None
                else stored[-1].turn_id
                if stored and stored[-1].turn_id is not None
                else codec.turn_id(message, current_turn, message_id)
            )
            group = codec.items_for_message(
                message,
                item_sequence=stored[0].item_sequence
                if stored
                else batch.next_item_sequence,
                message_id=message_id,
                turn_id=turn_id,
                timestamp=datetime.now(UTC).isoformat(),
            )
            # codec 自身验证 group 完整性；不能接受返回顺序与投影不一致的适配器。
            codec.project_message(group)
            if stored:
                _compare_group(group, stored)
            if not codec.is_internal(message):
                current_turn = turn_id
            batch.item_ids.extend(item.item_id for item in group)
            if indexed is not None:
                batch.visible_sequences.append(indexed[0])
                continue
            sequence = batch.last_message_sequence + 1
            locations: list[ItemLocation] = []
            raw_parts: list[bytes] = []
            if not stored:
                for item in group:
                    raw = canonical_json_line(item.to_dict())
                    locations.append((item, offset, len(raw)))
                    raw_parts.append(raw)
                    offset += len(raw)
                _, message_offset, message_length = locations[-1]
                batch.next_item_sequence += len(group)
            else:
                message_offset, message_length = catalog[:2]
            serialized = codec.project_message(stored or group)
            # 派生 message projection 也从唯一 canonical group 构造，不保存第二份正文。
            batch.prepared.append(
                (
                    sequence,
                    message_id,
                    turn_id,
                    codec.message_role(message),
                    canonical_json_text(serialized),
                    message_offset,
                    message_length,
                    b"".join(raw_parts),
                    "internal" if codec.is_internal(message) else "visible",
                    "{}",
                    tuple(locations),
                )
            )
            batch.last_message_sequence = sequence
            if batch.first_message_sequence is None:
                batch.first_message_sequence = sequence
            batch.visible_sequences.append(sequence)
    return batch
