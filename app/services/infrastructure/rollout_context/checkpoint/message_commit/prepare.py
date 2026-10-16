"""准备消息和 canonical group，写入动作仍由同一 checkpoint 事务完成。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.core.execution_admission_context import require_current_execution_admission
from app.domain.itemized.enums import PayloadKind
from app.domain.itemized.records import CanonicalItemRecord, RuntimeNoticePayload
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
    str | None,
    str,
    str,
    int,
    int,
    str,
    tuple[ItemLocation, ...],
    str,
]


@dataclass
class MessageBatch:
    next_item_sequence: int
    last_message_sequence: int
    first_message_sequence: int | None = None
    prepared: list[PreparedMessage] = field(default_factory=list)
    visible_sequences: list[int] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    display_only_item_ids: list[str] = field(default_factory=list)


def _compare_group(
    expected: Sequence[CanonicalItemRecord],
    actual: Sequence[CanonicalItemRecord],
) -> None:
    if len(expected) != len(actual):
        raise ValueError("canonical message group 成员数发生变化")
    for candidate, stored in zip(expected, actual, strict=True):
        differing = [
            name
            for name in (
                "item_id",
                "semantic_kind",
                "payload_kind",
                "content_hash",
                "turn_id",
                "turn_scope",
                "message_group_id",
            )
            if getattr(candidate, name) != getattr(stored, name)
        ]
        if differing:
            raise ValueError(
                f"canonical item 与 message projection 内容不一致: {stored.item_id}:"
                + ",".join(
                    f"{name}={getattr(candidate, name)!r}/{getattr(stored, name)!r}"
                    for name in differing
                )
            )
        if canonical_json_text(candidate.projection_manifest()) != canonical_json_text(
            stored.projection_manifest()
        ):
            raise ValueError(
                f"canonical item typed provenance/extensions 不一致: {stored.item_id}"
            )


def prepare_messages(
    *,
    connection: sqlite3.Connection,
    jsonl: Path,
    codec: MessageCodec,
    read_item: Callable[..., CanonicalItemRecord],
    thread_id: str,
    messages: Sequence[object],
    last_message_sequence: int,
    next_item_sequence: int,
    offset: int,
) -> MessageBatch:
    batch = MessageBatch(next_item_sequence, last_message_sequence)
    canonical_message_ids = [
        codec.message_id(message, index) for index, message in enumerate(messages)
    ]
    if len(set(canonical_message_ids)) != len(canonical_message_ids):
        raise ValueError("一个 checkpoint 不能重复引用同一个 canonical message_id")
    with jsonl.open("rb") as stream:
        for index, (canonical_message_id, message) in enumerate(
            zip(canonical_message_ids, messages, strict=True)
        ):
            message_locator = connection.execute(
                "SELECT message_sequence, turn_id, execution_id, jsonl_offset, jsonl_length FROM messages "
                "WHERE message_id = ?",
                (canonical_message_id,),
            ).fetchone()
            canonical_anchor = connection.execute(
                "SELECT jsonl_offset, jsonl_length, turn_id, execution_id "
                "FROM item_catalog WHERE jsonl_offset = ? AND jsonl_length = ?",
                message_locator[3:5],
            ).fetchone() if message_locator is not None else None
            if message_locator is not None and canonical_anchor is None:
                raise RuntimeError(
                    f"v2 message projection 缺少 canonical item: {canonical_message_id}"
                )
            message_item_group = (
                read_message_group(
                    connection,
                    stream,
                    offset=canonical_anchor[0],
                    length=canonical_anchor[1],
                    read_item=read_item,
                )
                if canonical_anchor is not None
                else ()
            )
            message_role = codec.message_role(message)
            if message_locator is not None:
                stored_turn_ids = {item.turn_id for item in message_item_group}
                if len(stored_turn_ids) != 1:
                    raise RuntimeError(
                        f"canonical message group Turn identity 不一致: {canonical_message_id}"
                    )
                stored_turn_id = next(iter(stored_turn_ids))
                stored_execution_ids: set[str] = set()
                for item in message_item_group:
                    canonical_item_id = item.item_id
                    execution_row = connection.execute(
                        "SELECT execution_id FROM item_catalog WHERE item_id = ?",
                        (canonical_item_id,),
                    ).fetchone()
                    if (
                        execution_row is None
                        or not isinstance(execution_row[0], str)
                        or not execution_row[0]
                    ):
                        raise RuntimeError(
                            f"canonical item 缺少 execution attribution: {canonical_item_id}"
                        )
                    stored_execution_ids.add(execution_row[0])
                if len(stored_execution_ids) != 1:
                    raise RuntimeError(
                        f"canonical message group execution identity 不一致: {canonical_message_id}"
                    )
                stored_execution_id = next(iter(stored_execution_ids))
                if canonical_anchor is None or (
                    canonical_anchor[2] != stored_turn_id or canonical_anchor[3] != stored_execution_id
                ):
                    raise RuntimeError(
                        "canonical item 与 message-root catalog attribution 不一致: "
                        f"{canonical_message_id}"
                    )
                if (
                    message_locator[1] != stored_turn_id or message_locator[2] != stored_execution_id
                ):
                    raise RuntimeError(
                        "message projection 与 canonical item attribution 不一致: "
                        f"{canonical_message_id}"
                    )
                turn_id = stored_turn_id
                execution_id = stored_execution_id
            else:
                admission = require_current_execution_admission(
                    langgraph_thread_id=thread_id
                )
                execution_id = admission.execution_id
                if admission.execution_origin == "user_turn":
                    if admission.turn_id is None:
                        raise RuntimeError(
                            "user_turn execution admission 缺少 accepted turn_id"
                        )
                    if message_role == "user" and codec.is_internal(message):
                        raise RuntimeError(
                            "user_turn execution admission 不得写入 internal HumanMessage；"
                            "内部通知必须使用 internal admission"
                        )
                    turn_id = admission.turn_id
                elif admission.execution_origin == "internal":
                    if admission.turn_id is not None:
                        raise RuntimeError(
                            "internal execution admission 不得绑定 turn_id"
                        )
                    turn_id = None
                else:
                    raise RuntimeError(
                        "未知 execution admission origin: "
                        f"{admission.execution_origin!r}"
                    )
            model_call_id = (
                codec.tool_message_model_call_id(message, messages[:index])
                if message_role == "tool"
                else codec.model_call_id(message)
                if message_role == "assistant"
                else None
            )
            group = codec.items_for_message(
                message,
                item_sequence=message_item_group[0].item_sequence
                if message_item_group
                else batch.next_item_sequence,
                message_id=canonical_message_id,
                turn_id=turn_id,
                execution_id=execution_id,
                timestamp=datetime.now(UTC).isoformat(),
                model_call_id=model_call_id,
                canonical_items=message_item_group,
            )
            # codec 自身验证 group 完整性；不能接受返回顺序与投影不一致的适配器。
            codec.project_message(group)
            if message_item_group:
                _compare_group(group, message_item_group)
            batch.item_ids.extend(item.item_id for item in group)
            for item in group:
                if item.turn_id is not None:
                    continue
                if item.semantic_kind == "runtime_notice":
                    if (
                        item.payload_kind != PayloadKind.STRUCTURED_CONTENT
                        or not isinstance(item.payload, Mapping)
                        or set(item.payload) != {"prompt", "display_part"}
                    ):
                        continue
                    RuntimeNoticePayload.from_dict(item.payload)
                if item.turn_scope is None or item.semantic_kind == "runtime_notice":
                    batch.display_only_item_ids.append(item.item_id)
            if message_locator is not None:
                # 候选组只验证内容与身份；已存消息继续引用原 locator，不写候选坐标。
                batch.visible_sequences.append(message_locator[0])
                continue
            sequence = batch.last_message_sequence + 1
            locations: list[ItemLocation] = []
            for item in group:
                raw = canonical_json_line(item.to_dict())
                locations.append((item, offset, len(raw)))
                offset += len(raw)
            _, message_offset, message_length = locations[-1]
            batch.next_item_sequence += len(group)
            serialized = codec.project_message(group)
            # 派生 message projection 也从唯一 canonical group 构造，不保存第二份正文。
            batch.prepared.append(
                (
                    sequence,
                    canonical_message_id,
                    turn_id,
                    codec.message_role(message),
                    canonical_json_text(serialized),
                    message_offset,
                    message_length,
                    "internal" if codec.is_internal(message) else "visible",
                    tuple(locations),
                    execution_id,
                )
            )
            batch.last_message_sequence = sequence
            if batch.first_message_sequence is None:
                batch.first_message_sequence = sequence
            batch.visible_sequences.append(sequence)
    return batch
