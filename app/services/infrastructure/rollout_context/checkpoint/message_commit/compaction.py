"""新压缩事件的 canonical 摘要与精确消息选择；提交仍归 persistence。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence

from app.core.execution_admission_context import require_current_execution_admission
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.message_commit.prepare import (
    MessageBatch,
)
from app.services.infrastructure.rollout_context.storage.primitives import MessageCodec
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
    canonical_json_text,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


def new_compaction_event(
    connection: sqlite3.Connection, checkpoint: Mapping[str, object],
    *, parent_checkpoint_id: str | None,
) -> Mapping[str, object] | None:
    """LastValue 留存不等于新事件；以父 checkpoint 的真实 channel version 判定。"""
    values = checkpoint.get("channel_values", {})
    versions = checkpoint.get("channel_versions", {})
    if not isinstance(values, Mapping) or not isinstance(versions, Mapping):
        raise TypeError("checkpoint channels 必须是 mapping")
    event = values.get("_summarization_event")
    if event is None:
        return None
    version = strict_text(versions.get("_summarization_event"), field="compaction channel version")
    parent = connection.execute(
        "SELECT channel_version FROM checkpoint_channels WHERE checkpoint_id=? AND channel_name='_summarization_event'",
        (parent_checkpoint_id,),
    ).fetchone() if parent_checkpoint_id is not None else None
    if parent is not None and parent[0] == version:
        return None
    if not isinstance(event, Mapping):
        raise TypeError("新 _summarization_event 必须是 mapping")
    return event


def prepare_compaction(
    *, connection: sqlite3.Connection, codec: MessageCodec, batch: MessageBatch,
    event: Mapping[str, object], messages: Sequence[object], thread_id: str,
    checkpoint_id: str, timestamp: str, offset: int,
) -> tuple[tuple[str, ...], int]:
    """摘要归属触发执行；原 messages channel 及 cutoff 坐标保持不变。"""
    cutoff = event.get("cutoff_index")
    if type(cutoff) is not int or not 0 <= cutoff <= len(messages):
        raise ValueError("compaction cutoff_index 超过真实 checkpoint messages")
    summary_message = event.get("summary_message")
    summary = codec.to_dict(summary_message)
    data = summary.get("data")
    if summary.get("type") != "human" or not isinstance(data, Mapping):
        raise TypeError("compaction summary_message 必须是 user 消息")
    summary_id = strict_text(data.get("id"), field="compaction summary_message.id")
    text = strict_text(data.get("content"), field="compaction summary_message.content")
    metadata = data.get("response_metadata", {})
    relations = metadata.get("provenance_relations", []) if isinstance(metadata, Mapping) else []
    if not isinstance(relations, list):
        raise TypeError("compaction summary provenance_relations 必须是 typed array")
    prefixes = event.get("cache_prefix_messages", []) if event.get("strategy") == "cache_preserving" else []
    if not isinstance(prefixes, (list, tuple)) or len(prefixes) > cutoff:
        raise ValueError("compaction cache_prefix_messages 必须位于真实 cutoff 之前")
    for index, prefix in enumerate(prefixes):
        if canonical_json_text(codec.to_dict(prefix)) != canonical_json_text(codec.to_dict(messages[index])):
            raise ValueError("compaction cache prefix 与冻结 messages 前缀不一致")
    cutoff_id = event.get("cutoff_message_id")
    if cutoff_id is not None and (cutoff == 0 or codec.message_id(messages[cutoff - 1], cutoff - 1) != cutoff_id):
        raise ValueError("compaction cutoff_message_id 与真实 cutoff_index 不一致")
    admission = require_current_execution_admission(langgraph_thread_id=thread_id)
    payload = {"summary_id": summary_id, "view_revision": checkpoint_id, "summary": text}
    item = CanonicalItemRecord.create(
        item_sequence=batch.next_item_sequence, item_id=f"item-{summary_id}",
        semantic_kind=SemanticKind.COMPACTION_SUMMARY, payload_kind=PayloadKind.SUMMARY,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "runtime", "producer_id": checkpoint_id,
                      "invocation_id": admission.execution_id},
        payload=payload, created_at=timestamp, wire_role="user",
        projection_identity=ItemProjectionIdentity(projection_message_id=summary_id),
        provenance_relations=tuple(ItemProvenanceRelation.from_dict(relation) for relation in relations),
        turn_id=admission.turn_id,
        turn_scope=TurnScope.TURN_MEMBER if admission.turn_id is not None else None,
    )
    existing = connection.execute(
        "SELECT message_sequence FROM messages WHERE message_id=?", (summary_id,),
    ).fetchone()
    if existing is not None:
        sequence = existing[0]
        stored = connection.execute(
            "SELECT content_hash, semantic_kind, turn_id, turn_scope, execution_id, producer_ref_json, projection_manifest_json "
            "FROM item_catalog WHERE item_id=?", (item.item_id,),
        ).fetchone()
        if stored != (item.content_hash, SemanticKind.COMPACTION_SUMMARY, item.turn_id, item.turn_scope,
                      admission.execution_id, canonical_json_text(item.producer_ref), canonical_json_text(item.projection_manifest())):
            raise ValueError("compaction summary identity 已用于不同正文、执行归属或 checkpoint")
    else:
        raw = canonical_json_line(item.to_dict())
        sequence = batch.last_message_sequence + 1
        batch.prepared.append((
            sequence, summary_id, admission.turn_id, "user", canonical_json_text(codec.project_message((item,))),
            offset, len(raw), "internal", ((item, offset, len(raw)),), admission.execution_id,
        ))
        batch.next_item_sequence += 1
        batch.last_message_sequence = sequence
        if batch.first_message_sequence is None:
            batch.first_message_sequence = sequence
        if admission.turn_id is None:
            batch.display_only_item_ids.append(item.item_id)
    ordered = [*prefixes, summary_message, *messages[cutoff:]]
    pending_by_message = {row[1]: tuple(location[0].item_id for location in row[8]) for row in batch.prepared}
    selection: list[str] = []
    for index, message in enumerate(ordered):
        message_id = codec.message_id(message, index)
        ids = pending_by_message.get(message_id)
        if ids is None:
            ids = tuple(row[0] for row in connection.execute(
                "SELECT item_id FROM item_catalog "
                "WHERE json_extract(projection_manifest_json,'$.projection_identity.projection_message_id')=? "
                "ORDER BY item_sequence", (message_id,),
            ).fetchall())
        if not ids:
            raise ValueError(f"compaction selection 缺少真实 canonical 来源: {message_id}")
        selection.extend(ids)
    if len(selection) != len(set(selection)):
        raise ValueError("compaction prefix/summary/tail 重复选入 canonical item")
    return tuple(selection), sequence
