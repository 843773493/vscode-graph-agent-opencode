"""消息组幂等校验与同 logical ID 的不可变版本生成。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from uuid import uuid4

from app.domain.itemized.identity.item_projection import ItemProvenanceRelation
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    producer_response_metadata,
)
from app.services.infrastructure.rollout_context.storage.primitives import MessageCodec
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)


def _group_conflict(
    expected: Sequence[CanonicalItemRecord],
    actual: Sequence[CanonicalItemRecord],
) -> str | None:
    if len(expected) != len(actual):
        return "canonical message group 成员数发生变化"
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
            return (
                f"canonical item 与 message projection 内容不一致: {stored.item_id}:"
                + ",".join(
                    f"{name}={getattr(candidate, name)!r}/{getattr(stored, name)!r}"
                    for name in differing
                )
            )
        if canonical_json_text(candidate.projection_manifest()) != canonical_json_text(
            stored.projection_manifest()
        ):
            return (
                f"canonical item typed provenance/extensions 不一致: {stored.item_id}"
            )

    return None

def resolve_message_group(
    connection: sqlite3.Connection, *, codec: MessageCodec, message: object,
    message_value: Mapping[str, object], explicit_anchor: str | None,
    stored_group: Sequence[CanonicalItemRecord], message_id: str,
    turn_id: str | None, execution_id: str, model_call_id: str | None,
    next_item_sequence: int, timestamp: str, allow_revisions: bool,
) -> tuple[tuple[CanonicalItemRecord, ...], bool]:
    created = not stored_group
    candidate_message = message
    stored_value = codec.project_message(stored_group) if stored_group else None
    body_changed = stored_group and any(
        message_value["data"].get(field) != stored_value["data"].get(field)
        for field in ("content", "tool_calls", "invalid_tool_calls", "artifact", "tool_call_id")
    )
    if body_changed and allow_revisions:
        # 镜像只用于定位旧版本；新正文重新产生自己的组和来源关系。
        data = dict(message_value["data"])
        data["response_metadata"] = producer_response_metadata(data.get("response_metadata", {}))
        candidate_message = codec.from_dict({**message_value, "data": data})
    group = codec.items_for_message(
        candidate_message,
        item_sequence=stored_group[0].item_sequence
        if stored_group
        else next_item_sequence,
        message_id=message_id,
        turn_id=turn_id,
        execution_id=execution_id,
        timestamp=timestamp,
        model_call_id=model_call_id,
        canonical_items=stored_group,
    )
    # codec 自身验证 group 完整性；不能接受返回顺序与投影不一致的适配器。
    codec.project_message(group)
    if stored_group:
        source = stored_group[-1]
        source_revisions = tuple(relation for relation in source.provenance_relations
                                 if relation.relation == "replaces" and relation.target_kind == "canonical_item")
        if explicit_anchor is None and source_revisions and not any(
            relation.relation == "replaces" and relation.target_kind == "canonical_item"
            for relation in group[-1].provenance_relations
        ):
            # 版本身份由 locator 固定，纯消息输入无需重复声明 owner 生成的来源。
            group = (*group[:-1], replace(group[-1], provenance_relations=(
                *group[-1].provenance_relations, *source_revisions,
            )))
        conflict = _group_conflict(group, stored_group)
        if conflict is not None:
            if not allow_revisions or (explicit_anchor is not None and not body_changed):
                raise ValueError(conflict)
            source_call = source.projection_identity.model_call_id if source.projection_identity is not None else None
            if not source_call:
                raise ValueError("canonical message revision 来源缺少真实 ModelCall")
            source_owner = connection.execute(
                "SELECT session_id,thread_id FROM executions WHERE execution_id=?", (execution_id,),
            ).fetchone()
            if source_owner is None:
                raise ValueError("canonical message revision 来源缺少 execution owner")
            relation = ItemProvenanceRelation(
                "replaces", "canonical_item", source.item_id,
                target_session_id=source_owner[0], target_thread_id=source_owner[1],
                target_turn_id=source.turn_id, target_model_call_id=source_call,
            )
            identity = uuid4().hex
            group = tuple(replace(
                item, item_sequence=next_item_sequence + ordinal,
                item_id=f"item-{identity}" if ordinal == len(group) - 1 else f"item-{identity}-member-{ordinal}",
                message_group_id=f"message-{identity}",
                provenance_relations=tuple(relation for relation in item.provenance_relations
                                           if not (relation.relation == "replaces" and relation.target_kind == "canonical_item"))
                + ((relation,) if ordinal == len(group) - 1 else ()),
            ) for ordinal, item in enumerate(group))
            codec.project_message(group)
            created = True
    return tuple(group), created
