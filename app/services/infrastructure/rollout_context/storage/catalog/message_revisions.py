"""不可变 canonical message revision 的精确来源校验。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.records import (
    CanonicalItemRecord,
    parse_item_projection_manifest,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


@dataclass(frozen=True, slots=True)
class MessageRevisionSource:
    """被替换 canonical anchor 的无正文、精确 locator 与归属。"""

    item_id: str
    item_sequence: int
    session_id: str
    thread_id: str
    turn_id: str | None
    execution_id: str
    model_call_id: str
    message_group_id: str


def validate_message_revision_source(
    connection: sqlite3.Connection,
    item: CanonicalItemRecord,
) -> MessageRevisionSource | None:
    """校验唯一 replaces/canonical_item 来源；普通 item 返回 None。

    调用方必须传入消息组 anchor。来源 owner、Turn、ModelCall 和 logical
    projection identity 均从指定 item_id 解析，不按 message ID 搜索版本。
    """
    replacements = tuple(
        relation for relation in item.provenance_relations
        if relation.relation == "replaces"
        and relation.target_kind == "canonical_item"
    )
    if not replacements:
        return None
    if len(replacements) != 1:
        raise ValueError("message revision 必须只有一个 replaces 来源")
    relation = replacements[0]
    if item.projection_group is not None and item.projection_group.ordinal != item.projection_group.size - 1:
        raise ValueError("message revision 必须由消息组 anchor 校验")
    if relation.target_id == item.item_id:
        raise ValueError("message revision 不得替换自身")
    if not relation.target_session_id or not relation.target_thread_id:
        raise ValueError("message revision 来源缺少完整 owner scope")
    if not relation.target_model_call_id:
        raise ValueError("message revision 来源缺少 source ModelCall")

    require_historical_scope(
        connection, relation.target_session_id, relation.target_thread_id,
    )
    row = connection.execute(
        "SELECT item.item_sequence,item.execution_id,item.turn_id,item.semantic_kind,"
        "item.turn_scope,item.message_group_id,item.projection_manifest_json,"
        "execution.session_id,execution.thread_id,call.model_call_id,call.execution_id,"
        "call.session_id,call.thread_id,call.assembly_id,assembly.execution_id,assembly.session_id,"
        "assembly.thread_id "
        "FROM item_catalog item "
        "LEFT JOIN executions execution ON execution.execution_id=item.execution_id "
        "LEFT JOIN model_calls call ON call.model_call_id=? "
        "LEFT JOIN context_assemblies assembly ON assembly.assembly_id=call.assembly_id "
        "WHERE item.item_id=?",
        (relation.target_model_call_id, relation.target_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"message revision source item 缺失: {relation.target_id}")

    source_sequence = strict_non_negative_int(
        row[0], field="message revision source.item_sequence",
    )
    item_sequence = strict_non_negative_int(
        item.item_sequence, field="message revision.item_sequence",
    )
    if source_sequence >= item_sequence:
        raise ValueError("message revision source sequence 必须严格早于新版本")
    if not item.message_group_id or item.message_group_id == row[5]:
        raise ValueError("message revision 必须使用独立的新消息组身份")
    if connection.execute(
        "SELECT 1 FROM item_catalog WHERE message_group_id=? AND item_id<>? "
        "AND (json_extract(projection_manifest_json,'$.projection_group') IS NULL "
        "OR json_extract(projection_manifest_json,'$.projection_group.ordinal')="
        "json_extract(projection_manifest_json,'$.projection_group.size')-1) LIMIT 1",
        (item.message_group_id, item.item_id),
    ).fetchone() is not None:
        raise ValueError("message revision 新消息组已被其它 anchor 占用")
    if row[1] is None or row[7:9] != (
        relation.target_session_id, relation.target_thread_id,
    ):
        raise ValueError("message revision source execution owner 不一致")
    source_execution_id = strict_text(row[1], field="message revision source.execution_id")
    source_turn_id = strict_optional_text(row[2], field="message revision source.turn_id")
    if relation.target_turn_id != source_turn_id or item.turn_id != source_turn_id:
        raise ValueError("message revision source Turn 与新版本不一致")

    if row[3] == "user_input" and row[4] == "turn_root":
        raise ValueError("accepted root canonical item 不得被替换")
    if connection.execute(
        "SELECT 1 FROM turn_records WHERE root_input_item_id=? LIMIT 1",
        (relation.target_id,),
    ).fetchone() is not None:
        raise ValueError("accepted root canonical item 不得被替换")

    raw_manifest = strict_text(
        row[6], field="message revision source.projection_manifest_json",
    )
    try:
        manifest_value = json.loads(raw_manifest)
    except json.JSONDecodeError as error:
        raise ValueError("message revision source projection manifest 非法") from error
    manifest = parse_item_projection_manifest(manifest_value)
    if canonical_json_text(manifest) != raw_manifest:
        raise ValueError("message revision source projection manifest 非 canonical JSON")
    source_group = manifest["projection_group"]
    if source_group is not None and source_group["ordinal"] != source_group["size"] - 1:
        raise ValueError("message revision source 必须是消息组 anchor")
    source_identity_value = manifest["projection_identity"]
    if not isinstance(source_identity_value, dict):
        raise TypeError("message revision source 缺少 projection identity")
    source_identity = ItemProjectionIdentity.from_dict(source_identity_value)
    current_identity = item.projection_identity
    if (
        current_identity is None
        or not isinstance(current_identity.projection_message_id, str)
        or not current_identity.projection_message_id
        or source_identity.projection_message_id != current_identity.projection_message_id
    ):
        raise ValueError("message revision source logical projection ID 不一致")
    if source_identity.model_call_id != relation.target_model_call_id:
        raise ValueError("message revision source ModelCall relation 不一致")
    if (
        not isinstance(row[5], str) or not row[5]
        or row[9] != relation.target_model_call_id
        or row[10] != source_execution_id
        or row[11:13] != (relation.target_session_id, relation.target_thread_id)
        or row[13] is None
        or row[14] != source_execution_id
        or row[15:17] != (relation.target_session_id, relation.target_thread_id)
    ):
        raise ValueError("message revision source ModelCall/assembly owner 不一致")

    return MessageRevisionSource(
        item_id=relation.target_id,
        item_sequence=source_sequence,
        session_id=relation.target_session_id,
        thread_id=relation.target_thread_id,
        turn_id=source_turn_id,
        execution_id=source_execution_id,
        model_call_id=relation.target_model_call_id,
        message_group_id=row[5],
    )
