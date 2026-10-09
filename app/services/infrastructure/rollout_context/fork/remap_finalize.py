"""full_rollout_copy 的投影刷新与 source lineage 记录。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from app.core.hashing import sha256_hex as _hash_bytes
from app.domain.itemized.hashing import (
    payload_content_length,
    sha256_jcs,
)
from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    json_mapping,
)


def _update_one(
    connection: object,
    sql: str,
    parameters: tuple[object, ...],
    *,
    context: str,
) -> None:
    result = connection.execute(sql, parameters)
    if result.rowcount != 1:
        raise RuntimeError(f"full_rollout_copy {context} 行数不一致: {result.rowcount}")


_VISIBLE_TEXT_LIMIT = 64 * 1024


def refresh_full_copy_projections(state: FullCopyRemapState) -> None:
    self = state.service
    connection = state.connection
    target_session_id = state.target_session_id
    checkpoint_ns = state.checkpoint_ns
    timestamp = state.timestamp
    item_rows = state.item_rows
    content_part_rows = state.content_part_rows
    part_maps = state.part_maps
    new_positions = state.new_positions
    new_items = state.new_items
    new_lines = state.new_lines
    new_lines_by_sequence = state.new_lines_by_sequence
    mapped = state.mapped
    overlay_epoch_map = state.overlay_epoch_map
    target_overlay_epoch = max(overlay_epoch_map.values(), default=0)
    target_thread_id = state.target_thread_id
    # message 通过权威 locator 引用 anchor；可空的展示 identity 不参与寻址。
    source_anchor_sequences = {
        position: sequence for sequence, position in state.old_positions.items()
    }
    if len(source_anchor_sequences) != len(state.old_positions):
        raise RuntimeError("full_rollout_copy source canonical locator 重复")
    _update_one(
        connection,
        "UPDATE database_meta SET session_id = ?, thread_id = ?, rollout_id = ?, "
        "active_branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?), "
        "committed_jsonl_offset = ?, source_overlay_epoch = ?, updated_at = ? "
        "WHERE singleton_id = 1",
        (
            target_session_id,
            target_thread_id,
            self._storage._rollout_id(
                target_session_id,
                thread_id=target_thread_id,
                checkpoint_ns=checkpoint_ns,
            ),
            checkpoint_ns,
            sum(len(line) for line in new_lines),
            target_overlay_epoch,
            timestamp,
        ),
        context="database_meta",
    )

    # checkpoint core 的 id 与 SQL 主键是同一身份，不能留下 source id 让下一次
    # fork 把已导入 checkpoint 误当成最早 source 的 checkpoint。
    for source_id, target_id in state.maps.get("checkpoint", {}).items():
        row = connection.execute(
            "SELECT checkpoint_json, metadata_json FROM checkpoints WHERE checkpoint_id = ?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"full copy target checkpoint 缺失: {target_id}")
        checkpoint = dict(json_mapping(row[0], field="checkpoints.checkpoint_json"))
        if checkpoint.get("id") != source_id:
            raise RuntimeError(
                f"full copy checkpoint core 与 source 主键不一致: {source_id}"
            )
        checkpoint["id"] = target_id
        metadata = json_mapping(row[1], field="checkpoints.metadata_json")
        _update_one(
            connection,
            "UPDATE checkpoints SET checkpoint_json = ?, metadata_json = ? WHERE checkpoint_id = ?",
            (_json(checkpoint), _json(state.remap_json(metadata)), target_id),
            context=f"checkpoint:{target_id}",
        )

    connection.execute("DELETE FROM item_relations WHERE canonical_item_id IS NOT NULL")
    for sequence, item in new_items.items():
        offset, length = new_positions[sequence]
        _update_one(
            connection,
            "UPDATE item_catalog SET jsonl_offset = ?, jsonl_length = ?, producer_ref_json = ?, payload_length = ?, source_revision = ?, content_hash = ?, projection_manifest_json = ?, tool_call_id = ? WHERE item_sequence = ?",
            (
                offset,
                length,
                _json(item.producer_ref),
                payload_content_length(item.payload_kind, item.payload),
                (
                    optional_text(
                        (
                            item.source_manifest.source_revision
                            if item.source_manifest
                            else None
                        ),
                        field=f"item_catalog.source_revision:{item.item_id}",
                    )
                    or f"canonical:{item.item_id}:{item.content_hash}"
                ),
                item.content_hash,
                _json(item.projection_manifest()),
                item.tool_call_id,
                sequence,
            ),
            context=f"item_catalog:{sequence}",
        )
        self._storage._insert_canonical_relations(connection, item)
        projection_content = self._storage._codec().projection_content(item)
        _update_one(
            connection,
            "UPDATE item_projections SET content = ?, content_length = ?, content_truncated = ?, content_hash = ?, updated_at = ? WHERE item_sequence = ?",
            (
                projection_content[:_VISIBLE_TEXT_LIMIT],
                len(projection_content),
                int(len(projection_content) > _VISIBLE_TEXT_LIMIT),
                item.content_hash,
                timestamp,
                sequence,
            ),
            context=f"item_projections:{sequence}",
        )

    for (
        sequence,
        message_id,
        _turn_id,
        _offset,
        _length,
        _content_length,
        _content_hash,
        *_rest,
    ) in connection.execute(
        "SELECT message_sequence, message_id, turn_id, jsonl_offset, jsonl_length, content_length, content_hash, visibility, commit_id, created_at FROM messages ORDER BY message_sequence"
    ).fetchall():
        sequence = non_negative_int(sequence, field="messages.message_sequence")
        if sequence == 0:
            raise RuntimeError("full_rollout_copy messages.message_sequence 不能为 0")
        message_id = required_text(message_id, field="messages.message_id")
        optional_text(_turn_id, field=f"messages.turn_id:{message_id}")
        message_offset = non_negative_int(
            _offset, field=f"messages.jsonl_offset:{message_id}"
        )
        message_length = non_negative_int(
            _length, field=f"messages.jsonl_length:{message_id}"
        )
        if message_length == 0:
            raise RuntimeError(
                f"full_rollout_copy message JSONL length 为 0: {message_id}"
            )
        non_negative_int(_content_length, field=f"messages.content_length:{message_id}")
        required_text(_content_hash, field=f"messages.content_hash:{message_id}")
        required_text(_rest[0], field=f"messages.visibility:{message_id}")
        non_negative_int(_rest[1], field=f"messages.commit_id:{message_id}")
        required_text(_rest[2], field=f"messages.created_at:{message_id}")
        anchor_sequence = source_anchor_sequences.get((message_offset, message_length))
        if anchor_sequence is None:
            raise RuntimeError(
                "full_rollout_copy message 没有对应 canonical item: "
                f"message_id={message_id}"
            )
        item = new_items[anchor_sequence]
        if item.turn_id != _turn_id:
            raise RuntimeError(
                f"full copy message Turn 与 target canonical 不一致: {message_id}"
            )
        offset, length = new_positions[item.item_sequence]
        group = (
            tuple(
                value
                for value in sorted(
                    new_items.values(), key=lambda value: value.item_sequence
                )
                if value.message_group_id == item.message_group_id
            )
            if item.projection_group is not None
            else (item,)
        )
        message = self._storage._codec().project_message(group)
        content = (
            message.get("data", {}).get("content")
            if isinstance(message.get("data"), Mapping)
            else None
        )
        content_raw = _json(content).encode("utf-8")
        _update_one(
            connection,
            "UPDATE messages SET jsonl_offset = ?, jsonl_length = ?, content_length = ?, content_hash = ? WHERE message_sequence = ?",
            (
                offset,
                length,
                len(content_raw),
                _hash_bytes(content_raw),
                sequence,
            ),
            context=f"messages:{message_id}",
        )

    item_by_source_id = {
        required_text(row[1], field="item_catalog.item_id"): new_items[
            non_negative_int(row[0], field="item_catalog.item_sequence")
        ]
        for row in item_rows
    }
    for old_item_id, old_part_id in content_part_rows:
        old_item_id = required_text(old_item_id, field="item_parts.item_id")
        old_part_id = required_text(old_part_id, field="item_parts.part_id")
        item = item_by_source_id.get(old_item_id)
        if item is None:
            raise RuntimeError(f"full_rollout_copy item part 缺少 item: {old_item_id}")
        new_part_id = required_text(
            part_maps.get((old_item_id, old_part_id)),
            field=f"item_parts.target_part_id:{old_item_id}/{old_part_id}",
        )
        target_item_id = required_text(
            mapped("item", old_item_id),
            field=f"item_parts.target_item_id:{old_item_id}",
        )
        part_row = connection.execute(
            "SELECT locator_json FROM item_parts WHERE item_id = ? AND part_id = ?",
            (target_item_id, new_part_id),
        ).fetchone()
        if part_row is None:
            raise RuntimeError(
                "full_rollout_copy item part remap 后行缺失: "
                f"{target_item_id}/{new_part_id}"
            )
        locator_text = required_text(
            part_row[0],
            field=f"item_parts.locator_json:{target_item_id}/{new_part_id}",
        )
        try:
            locator = json.loads(locator_text)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"full_rollout_copy item part locator JSON 非法: {target_item_id}/{new_part_id}"
            ) from error
        if not isinstance(locator, Mapping):
            raise TypeError(
                "full_rollout_copy item part locator 必须是 object: "
                f"{target_item_id}/{new_part_id}"
            )
        selected = item.payload
        part_hash = sha256_jcs(selected)
        _update_one(
            connection,
            "UPDATE item_parts SET content_hash = ?, line_hash = ? WHERE item_id = ? AND part_id = ?",
            (
                part_hash,
                _hash_bytes(new_lines_by_sequence[item.item_sequence]),
                target_item_id,
                new_part_id,
            ),
            context=f"item_parts:{target_item_id}/{new_part_id}",
        )
