"""full_rollout_copy 的投影刷新与 source lineage 记录。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from uuid import uuid4

from app.domain.itemized.hashing import (
    canonical_json_bytes,
    payload_content_length,
    sha256_jcs,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
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


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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
    _update_one(
        connection,
        "UPDATE database_meta SET session_id = ?, rollout_id = ?, active_branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?), committed_jsonl_offset = ?, source_overlay_epoch = ?, updated_at = ? WHERE singleton_id = 1",
        (
            target_session_id,
            self._rollout_id(target_session_id),
            checkpoint_ns,
            sum(len(line) for line in new_lines),
            target_overlay_epoch,
            timestamp,
        ),
        context="database_meta",
    )

    for sequence, item in new_items.items():
        offset, length = new_positions[sequence]
        _update_one(
            connection,
            "UPDATE item_catalog SET jsonl_offset = ?, jsonl_length = ?, producer_ref_json = ?, payload_length = ?, source_revision = ?, content_hash = ?, metadata_json = ? WHERE item_sequence = ?",
            (
                offset,
                length,
                _json(item.producer_ref),
                payload_content_length(item.payload_kind, item.payload),
                (
                    optional_text(
                        item.metadata.get("source_revision"),
                        field=f"item_catalog.source_revision:{item.item_id}",
                    )
                    or f"canonical:{item.item_id}:{item.content_hash}"
                ),
                item.content_hash,
                _json(item.metadata),
                sequence,
            ),
            context=f"item_catalog:{sequence}",
        )
        projection_content = self._codec().projection_content(item)
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
        required_text(_turn_id, field=f"messages.turn_id:{message_id}")
        non_negative_int(_offset, field=f"messages.jsonl_offset:{message_id}")
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
        item = next(
            (
                value
                for value in new_items.values()
                if value.metadata.get("projection_message_id") == message_id
            ),
            None,
        )
        if item is None:
            raise RuntimeError(
                "full_rollout_copy message 没有对应 canonical item: "
                f"message_id={message_id}"
            )
        offset, length = new_positions[item.item_sequence]
        group = tuple(
            value for value in sorted(new_items.values(), key=lambda value: value.item_sequence)
            if value.message_group_id == item.message_group_id
        ) if item.metadata.get("projection_group") is not None else (item,)
        message = self._codec().project_message(group)
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


def record_full_copy_lineage(state: FullCopyRemapState) -> None:
    connection = state.connection
    source_session_id = state.source_session_id
    target_session_id = state.target_session_id
    fork_id = state.fork_id
    timestamp = state.timestamp
    maps = state.maps
    item_rows = state.item_rows
    new_positions = state.new_positions
    part_maps = state.part_maps
    detail_rows = state.detail_rows
    overlay_epoch_map = state.overlay_epoch_map
    source_epoch_by_target = {
        target_epoch: source_epoch
        for source_epoch, target_epoch in overlay_epoch_map.items()
    }

    ordinal_specs = {
        "turn": ("turn_records", "turn_id", "turn_ordinal"),
        "execution": ("executions", "execution_id", "execution_ordinal"),
        "model_call": ("model_calls", "model_call_id", "attempt_ordinal"),
        "content_part": ("item_parts", "part_id", "part_ordinal"),
    }

    def target_ordinal(entity_type: str, target_id: str) -> int | None:
        spec = ordinal_specs.get(entity_type)
        if spec is None:
            return None
        target_id = required_text(target_id, field=f"{entity_type}.target_id")
        table, id_column, ordinal_column = spec
        if entity_type == "content_part":
            target_parts = []
            for (source_item_id, source_part_id), target_part_id in part_maps.items():
                source_item_id = required_text(
                    source_item_id, field="item_parts.source_item_id"
                )
                source_part_id = required_text(
                    source_part_id, field="item_parts.source_part_id"
                )
                target_part_id = required_text(
                    target_part_id, field="item_parts.target_part_id"
                )
                target_item_id = maps.get("item", {}).get(source_item_id)
                if target_item_id is None:
                    continue
                if f"{target_item_id}:{target_part_id}" == target_id:
                    target_parts.append((target_item_id, target_part_id))
            if len(target_parts) != 1:
                raise RuntimeError(
                    "full_rollout_copy content_part target identity 不唯一: "
                    f"{target_id}"
                )
            item_id, part_id = target_parts[0]
            row = connection.execute(
                f"SELECT {ordinal_column} FROM {table} WHERE item_id = ? AND {id_column} = ?",
                (item_id, part_id),
            ).fetchone()
        else:
            row = connection.execute(
                f"SELECT {ordinal_column} FROM {table} WHERE {id_column} = ?",
                (target_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(
                f"full_rollout_copy lineage target row 缺失: {entity_type}={target_id}"
            )
        ordinal = non_negative_int(
            row[0], field=f"{table}.{ordinal_column}:{target_id}"
        )
        return ordinal

    def inverse_map(entity_type: str, target_id: object) -> str | None:
        if target_id is None:
            return None
        target_text = required_text(target_id, field=f"{entity_type}.target_id")
        for source_id, mapped_id in maps.get(entity_type, {}).items():
            required_text(source_id, field=f"{entity_type}.source_id")
            required_text(mapped_id, field=f"{entity_type}.mapped_id")
        return next(
            (
                source_id
                for source_id, mapped_id in maps.get(entity_type, {}).items()
                if mapped_id == target_text
            ),
            target_text,
        )

    def item_coordinates(
        entity_type: str,
        source_id: str,
        target_id: str,
    ) -> tuple[int | None, int | None]:
        source_id = required_text(source_id, field=f"{entity_type}.source_id")
        target_id = required_text(target_id, field=f"{entity_type}.target_id")
        item_by_source_id = {
            required_text(row[1], field="item_catalog.item_id"): (
                non_negative_int(row[0], field="item_catalog.item_sequence"),
                non_negative_int(row[3], field="item_catalog.jsonl_offset"),
            )
            for row in item_rows
        }
        if entity_type == "item":
            item_coordinate = item_by_source_id.get(source_id)
            if item_coordinate is None:
                raise RuntimeError(
                    f"full_rollout_copy item lineage source row 缺失: {source_id}"
                )
            sequence, source_offset = item_coordinate
            target_position = new_positions.get(sequence)
            if target_position is None:
                raise RuntimeError(
                    f"full_rollout_copy item lineage target position 缺失: {source_id}"
                )
            target_offset = non_negative_int(
                target_position[0], field=f"new_positions.offset:{sequence}"
            )
            return source_offset, target_offset
        if entity_type == "content_part" and ":" in source_id:
            source_parts = []
            for (
                candidate_item_id,
                candidate_part_id,
            ), candidate_target_part_id in part_maps.items():
                candidate_item_id = required_text(
                    candidate_item_id, field="item_parts.source_item_id"
                )
                candidate_part_id = required_text(
                    candidate_part_id, field="item_parts.source_part_id"
                )
                candidate_target_part_id = required_text(
                    candidate_target_part_id, field="item_parts.target_part_id"
                )
                if f"{candidate_item_id}:{candidate_part_id}" == source_id:
                    source_parts.append(
                        (
                            candidate_item_id,
                            candidate_part_id,
                            candidate_target_part_id,
                        )
                    )
            if len(source_parts) != 1:
                raise RuntimeError(
                    "full_rollout_copy content_part source identity 不唯一: "
                    f"{source_id}"
                )
            source_item_id, _source_part_id, target_part_id = source_parts[0]
            target_item_id = required_text(
                maps.get("item", {}).get(source_item_id),
                field=f"item_parts.target_item_id:{source_item_id}",
            )
            expected_target_id = f"{target_item_id}:{target_part_id}"
            if expected_target_id != target_id:
                raise RuntimeError(
                    "full_rollout_copy content_part source/target identity 不匹配: "
                    f"source={source_id}, target={target_id}"
                )
            item_coordinate = item_by_source_id.get(source_item_id)
            if item_coordinate is None:
                raise RuntimeError(
                    f"full_rollout_copy content_part source item 缺失: {source_item_id}"
                )
            source_sequence, source_offset = item_coordinate
            target_position = new_positions.get(source_sequence)
            if target_position is None:
                raise RuntimeError(
                    f"full_rollout_copy content_part target position 缺失: {target_id}"
                )
            return (
                source_offset,
                non_negative_int(
                    target_position[0],
                    field=f"new_positions.offset:{source_sequence}",
                ),
            )
        return None, None

    def insert_lineage(
        entity_type: str,
        source_id: str,
        target_id: str,
        *,
        source_offset: int | None = None,
        target_offset: int | None = None,
        ordinal: int | None = None,
        lineage_extra: Mapping[str, object] | None = None,
    ) -> None:
        lineage = {
            "identity_mode": "remapped_target_local",
            "fork_id": fork_id,
            "source": {
                "session_id": source_session_id,
                "local_id": source_id,
                "offset": source_offset,
                "ordinal": ordinal,
            },
            "target": {
                "session_id": target_session_id,
                "local_id": target_id,
                "offset": target_offset,
                "ordinal": ordinal,
            },
        }
        if lineage_extra:
            lineage.update(dict(lineage_extra))
        result = connection.execute(
            "INSERT INTO fork_identity_mappings(mapping_id, fork_id, source_session_id, target_session_id, entity_type, source_local_id, target_local_id, source_offset, target_offset, lineage_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                fork_id,
                source_session_id,
                target_session_id,
                entity_type,
                source_id,
                target_id,
                source_offset,
                target_offset,
                _json(lineage),
                timestamp,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"full_rollout_copy lineage 写入失败: {entity_type}={source_id}"
            )

    for entity_type, entity_map in maps.items():
        # acceptance identity 在本事务开头由
        # ``_map_copied_acceptance_identities`` 写入；它们同时需要
        # 更新 copied acceptance/commit 的 key，不能在这里再插入
        # 第二份相同 mapping。
        if entity_type in {
            "accepted_ingress",
            "acceptance_idempotency_key",
        }:
            continue
        for source_id, target_id in entity_map.items():
            source_offset, target_offset = item_coordinates(
                entity_type,
                source_id,
                target_id,
            )
            ordinal = target_ordinal(entity_type, target_id)
            lineage_extra: dict[str, object] = {}
            if entity_type == "source_overlay":
                overlay = connection.execute(
                    "SELECT source_overlay_epoch, base_ref, delta_ref, base_source_revision, delta_source_revision FROM source_overlays WHERE overlay_id = ?",
                    (target_id,),
                ).fetchone()
                if overlay is None:
                    raise RuntimeError(
                        f"full_rollout_copy source overlay target 行缺失: {target_id}"
                    )
                target_epoch = non_negative_int(
                    overlay[0],
                    field=f"source_overlays.source_overlay_epoch:{target_id}",
                )
                source_epoch = source_epoch_by_target.get(target_epoch)
                if source_epoch is None:
                    raise RuntimeError(
                        f"full_rollout_copy source overlay epoch mapping 缺失: {target_epoch}"
                    )
                target_base_ref = optional_text(
                    overlay[1], field=f"source_overlays.base_ref:{target_id}"
                )
                target_delta_ref = optional_text(
                    overlay[2], field=f"source_overlays.delta_ref:{target_id}"
                )
                lineage_extra["source_overlay"] = {
                    "source_overlay_epoch": source_epoch,
                    "target_overlay_epoch": target_epoch,
                    "source_base_ref": inverse_map("request_ref", target_base_ref),
                    "source_delta_ref": inverse_map("request_ref", target_delta_ref),
                    "target_base_ref": target_base_ref,
                    "target_delta_ref": target_delta_ref,
                    "source_base_revision": optional_text(
                        overlay[3],
                        field=f"source_overlays.base_source_revision:{target_id}",
                    ),
                    "target_base_revision": optional_text(
                        overlay[3],
                        field=f"source_overlays.base_source_revision:{target_id}",
                    ),
                    "source_delta_revision": optional_text(
                        overlay[4],
                        field=f"source_overlays.delta_source_revision:{target_id}",
                    ),
                    "target_delta_revision": optional_text(
                        overlay[4],
                        field=f"source_overlays.delta_source_revision:{target_id}",
                    ),
                }
            elif entity_type == "detail":
                source_detail = detail_ref_from_key(source_id)
                target_detail = detail_ref_from_key(target_id)
                source_detail.require_owner(source_session_id)
                target_detail.require_owner(target_session_id)
                source_path = None
                source_detail_rows = 0
                target_path = connection.execute(
                    "SELECT relative_path FROM context_plan_details WHERE detail_ref = ?",
                    (target_id,),
                ).fetchone()
                if target_path is None:
                    raise RuntimeError(
                        f"full_rollout_copy detail target 行缺失: {target_id}"
                    )
                source_path = next(
                    (
                        required_text(
                            row[2], field="context_plan_details.relative_path"
                        )
                        for row in detail_rows
                        if required_text(
                            row[0], field="context_plan_details.detail_ref"
                        )
                        == source_id
                    ),
                    None,
                )
                source_detail_rows = sum(
                    1
                    for row in detail_rows
                    if required_text(row[0], field="context_plan_details.detail_ref")
                    == source_id
                )
                if source_detail_rows != 1 or source_path is None:
                    raise RuntimeError(
                        f"full_rollout_copy detail source 行缺失或重复: {source_id}"
                    )
                target_path_text = required_text(
                    target_path[0],
                    field=f"context_plan_details.relative_path:{target_id}",
                )
                if (
                    source_path != detail_relative_path(source_detail).as_posix()
                    or target_path_text
                    != detail_relative_path(target_detail).as_posix()
                ):
                    raise RuntimeError(
                        "source-mismatch: fork detail lineage locator 与 typed owner 不一致"
                    )
                lineage_extra["detail"] = {
                    "source_detail_ref": source_detail.to_dict(),
                    "target_detail_ref": target_detail.to_dict(),
                    "source_relative_path": source_path,
                    "target_relative_path": target_path_text,
                    "localization": "target_session_node",
                }
            insert_lineage(
                entity_type,
                source_id,
                target_id,
                source_offset=source_offset,
                target_offset=target_offset,
                ordinal=ordinal,
                lineage_extra=lineage_extra,
            )

    for (source_item_id, source_part_id), target_part_id in part_maps.items():
        source_item_id = required_text(
            source_item_id, field="item_parts.source_item_id"
        )
        source_part_id = required_text(
            source_part_id, field="item_parts.source_part_id"
        )
        target_part_id = required_text(
            target_part_id, field="item_parts.target_part_id"
        )
        target_item_id = next(
            (
                target_id
                for source_id, target_id in maps.get("item", {}).items()
                if source_id == source_item_id
            ),
            None,
        )
        target_item_id = required_text(
            target_item_id, field=f"item_parts.target_item_id:{source_item_id}"
        )
        source_local_id = f"{source_item_id}:{source_part_id}"
        target_local_id = f"{target_item_id}:{target_part_id}"
        source_offset, target_offset = item_coordinates(
            "content_part",
            source_local_id,
            target_local_id,
        )
        ordinal = target_ordinal("content_part", target_local_id)
        insert_lineage(
            "content_part",
            source_local_id,
            target_local_id,
            source_offset=source_offset,
            target_offset=target_offset,
            ordinal=ordinal,
        )

    for view_id, turn_id, ordinal in connection.execute(
        "SELECT view_id, turn_id, logical_turn_ordinal FROM context_view_turns ORDER BY view_id, logical_turn_ordinal"
    ).fetchall():
        view_id = required_text(view_id, field="context_view_turns.view_id")
        turn_id = required_text(turn_id, field="context_view_turns.turn_id")
        ordinal = non_negative_int(
            ordinal, field=f"context_view_turns.logical_turn_ordinal:{view_id}"
        )
        if ordinal == 0:
            raise RuntimeError(
                f"full_rollout_copy view Turn ordinal 不能为 0: {view_id}"
            )
        source_view_id = inverse_map("view", view_id)
        source_turn_id = inverse_map("turn", turn_id)
        if source_view_id is None or source_turn_id is None:
            raise RuntimeError(
                f"full_rollout_copy view Turn lineage source mapping 缺失: {view_id}/{turn_id}"
            )
        insert_lineage(
            "view_turn",
            f"{source_view_id}:{source_turn_id}",
            f"{view_id}:{turn_id}",
            ordinal=ordinal,
        )

    for view_id, item_id, ordinal in connection.execute(
        "SELECT view_id, item_id, logical_item_ordinal FROM context_view_items ORDER BY view_id, logical_item_ordinal"
    ).fetchall():
        view_id = required_text(view_id, field="context_view_items.view_id")
        item_id = required_text(item_id, field="context_view_items.item_id")
        ordinal = non_negative_int(
            ordinal, field=f"context_view_items.logical_item_ordinal:{view_id}"
        )
        source_view_id = inverse_map("view", view_id)
        source_item_id = inverse_map("item", item_id)
        if source_view_id is None or source_item_id is None:
            raise RuntimeError(
                f"full_rollout_copy view item lineage source mapping 缺失: {view_id}/{item_id}"
            )
        source_offset, target_offset = item_coordinates("item", source_item_id, item_id)
        insert_lineage(
            "view_item",
            f"{source_view_id}:{source_item_id}",
            f"{view_id}:{item_id}",
            source_offset=source_offset,
            target_offset=target_offset,
            ordinal=ordinal,
        )
