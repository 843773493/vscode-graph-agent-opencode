"""full_rollout_copy 的 SQLite identity/reference 收敛。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)
from app.services.infrastructure.rollout_context.fork.sql.common import (
    _hash_bytes,
    _json,
    rewrite_column,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
    one_of_text,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    validate_commit_contract,
)


def rewrite_full_copy_control_and_commits(state: FullCopyRemapState) -> None:
    connection = state.connection
    target_session_id = state.target_session_id
    fork_id = state.fork_id
    maps = state.maps
    part_maps = state.part_maps
    new_positions = state.new_positions
    mapped = state.mapped
    remap_json = state.remap_json
    overlay_epoch_map = state.overlay_epoch_map
    snapshot_count_row = connection.execute(
        "SELECT COUNT(*) FROM tool_set_snapshots"
    ).fetchone()
    if snapshot_count_row is None:
        raise RuntimeError("full_rollout_copy tool set snapshot 行数读取失败")
    expected_snapshot_count = non_negative_int(
        snapshot_count_row[0], field="tool_set_snapshots.match_count"
    )
    result = connection.execute(
        "UPDATE tool_set_snapshots SET session_id = ?",
        (target_session_id,),
    )
    if result.rowcount != expected_snapshot_count:
        raise RuntimeError("full_rollout_copy tool set snapshot remap 行数不一致")
    for entity, column in (("plan", "plan_id"), ("assembly", "assembly_id")):
        for old_id, new_id in maps.get(entity, {}).items():
            rewrite_column(connection, "tool_set_snapshots", column, old_id, new_id)

    for old_id, new_id in maps.get("group", {}).items():
        rewrite_column(
            connection,
            "item_catalog",
            "message_group_id",
            old_id,
            new_id,
        )

    for old_id, new_id in maps.get("plan", {}).items():
        rewrite_column(connection, "context_assemblies", "plan_id", old_id, new_id)

    for old_id, new_id in maps.get("detail", {}).items():
        rewrite_column(
            connection,
            "context_assemblies",
            "detail_ref",
            old_id,
            new_id,
        )

    for source_epoch, target_epoch in overlay_epoch_map.items():
        for table in ("context_assemblies", "context_assembly_selections"):
            count_row = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE source_overlay_epoch = ?",
                (source_epoch,),
            ).fetchone()
            if count_row is None:
                raise RuntimeError(
                    f"full_rollout_copy {table} overlay epoch 行数读取失败"
                )
            expected = non_negative_int(
                count_row[0], field=f"{table}.source_overlay_epoch.match_count"
            )
            result = connection.execute(
                f"UPDATE {table} SET source_overlay_epoch = ? WHERE source_overlay_epoch = ?",
                (target_epoch, source_epoch),
            )
            if result.rowcount != expected:
                raise RuntimeError(
                    f"full_rollout_copy {table} overlay epoch remap 行数不一致"
                )

    # item_id/part_id 已在 fast remap 阶段完成。这里不再重复写主键，避免
    # “第一次写成功、第二次找不到 source key”被误判为正常幂等。
    if part_maps and not new_positions:
        raise RuntimeError("full_rollout_copy part remap 缺少 item position")

    control_rows = connection.execute(
        "SELECT control_sequence, control_id, control_kind, entity_type, entity_id, branch_id, view_id, checkpoint_id, payload_json, transaction_id FROM control_events ORDER BY control_sequence"
    ).fetchall()

    previous_hash = ""

    for (
        control_sequence,
        control_id,
        control_kind,
        entity_type,
        entity_id,
        branch_id,
        view_id,
        checkpoint_id,
        payload_json,
        _control_transaction_id,
    ) in control_rows:
        control_sequence = non_negative_int(
            control_sequence, field="control_events.control_sequence"
        )
        if control_sequence == 0:
            raise RuntimeError("control_events.control_sequence 不能为 0")
        control_id = required_text(control_id, field="control_events.control_id")
        control_kind = required_text(control_kind, field="control_events.control_kind")
        entity_type = one_of_text(
            entity_type,
            {
                "item",
                "turn",
                "execution",
                "model_call",
                "assembly",
                "view",
                "branch",
                "checkpoint",
                "fork",
                "rollout",
                "schema",
            },
            field="control_events.entity_type",
        )
        entity_id = required_text(entity_id, field="control_events.entity_id")
        branch_id = optional_text(branch_id, field="control_events.branch_id")
        view_id = optional_text(view_id, field="control_events.view_id")
        checkpoint_id = optional_text(
            checkpoint_id, field="control_events.checkpoint_id"
        )
        payload_text = required_text(payload_json, field="control_events.payload_json")
        required_text(_control_transaction_id, field="control_events.transaction_id")
        try:
            payload_value = json.loads(payload_text)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"full_rollout_copy control payload JSON 非法: {control_sequence}"
            ) from error
        if not isinstance(payload_value, Mapping):
            raise TypeError(
                f"full_rollout_copy control payload 必须是 object: {control_sequence}"
            )
        if _json(payload_value) != payload_text:
            raise RuntimeError(
                f"full_rollout_copy control payload 不是 canonical JSON: {control_sequence}"
            )
        entity_map = {
            "item": "item",
            "turn": "turn",
            "execution": "execution",
            "model_call": "model_call",
            "assembly": "assembly",
            "view": "view",
            "branch": "branch",
            "checkpoint": "checkpoint",
            "fork": None,
            "rollout": None,
            "schema": None,
        }[entity_type]
        if entity_type == "fork":
            target_entity_id = fork_id
        elif entity_type == "rollout":
            target_entity_id = target_session_id
        else:
            target_entity_id = mapped(entity_map, entity_id)
            if target_entity_id is None:
                raise RuntimeError(
                    f"full_rollout_copy control entity mapping 缺失: {entity_type}/{entity_id}"
                )
        target_branch = mapped("branch", branch_id)
        target_view = mapped("view", view_id)
        target_checkpoint = mapped("checkpoint", checkpoint_id)
        remapped_payload = remap_json(payload_value)
        if not isinstance(remapped_payload, Mapping):
            raise TypeError(
                f"full_rollout_copy control payload remap 必须是 object: {control_sequence}"
            )
        target_control_id = mapped("control", control_id)
        if target_control_id is None:
            raise RuntimeError(
                f"full_rollout_copy control identity mapping 缺失: {control_id}"
            )
        event_hash = _hash_bytes(
            _json(
                {
                    "kind": control_kind,
                    "entity": target_entity_id,
                    "payload": remapped_payload,
                    "previous": previous_hash,
                }
            ).encode()
        )
        result = connection.execute(
            "UPDATE control_events SET control_id = ?, entity_id = ?, branch_id = ?, view_id = ?, checkpoint_id = ?, payload_json = ?, previous_event_hash = ?, event_hash = ? WHERE control_sequence = ?",
            (
                target_control_id,
                target_entity_id,
                target_branch,
                target_view,
                target_checkpoint,
                _json(remapped_payload),
                previous_hash or None,
                event_hash,
                control_sequence,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"full_rollout_copy control remap 行数不一致: {control_sequence}"
            )
        previous_hash = event_hash

    commit_rows = connection.execute(
        "SELECT commit_id, commit_kind, commit_mode, subject_id, idempotency_key, outcome, metadata_json FROM storage_commits ORDER BY commit_id"
    ).fetchall()

    current_offset = 0

    for (
        commit_id_value,
        commit_kind,
        commit_mode,
        subject_id,
        idempotency_key,
        outcome,
        metadata_json,
    ) in commit_rows:
        commit_id_value = non_negative_int(
            commit_id_value, field="storage_commits.commit_id"
        )
        commit_kind = one_of_text(
            commit_kind,
            {
                "acceptance",
                "item_convergence",
                "terminal_convergence",
                "assembly_sealed",
            },
            field="storage_commits.commit_kind",
        )
        commit_mode = one_of_text(
            commit_mode,
            {"item_bearing", "metadata_only"},
            field="storage_commits.commit_mode",
        )
        subject_id = optional_text(subject_id, field="storage_commits.subject_id")
        idempotency_key = optional_text(
            idempotency_key, field="storage_commits.idempotency_key"
        )
        outcome = optional_text(outcome, field="storage_commits.outcome")
        metadata_text = required_text(
            metadata_json, field="storage_commits.metadata_json"
        )
        try:
            metadata_value = json.loads(metadata_text)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"full_rollout_copy storage commit metadata JSON 非法: {commit_id_value}"
            ) from error
        if not isinstance(metadata_value, Mapping):
            raise TypeError(
                f"full_rollout_copy storage commit metadata 必须是 object: {commit_id_value}"
            )
        if _json(metadata_value) != metadata_text:
            raise RuntimeError(
                f"full_rollout_copy storage commit metadata 不是 canonical JSON: {commit_id_value}"
            )
        item_rows_for_commit = connection.execute(
            "SELECT item_sequence FROM item_catalog WHERE commit_id = ? ORDER BY item_sequence",
            (commit_id_value,),
        ).fetchall()
        item_positions: list[tuple[int, int]] = []
        for (raw_sequence,) in item_rows_for_commit:
            sequence = non_negative_int(
                raw_sequence, field="item_catalog.item_sequence"
            )
            if sequence not in new_positions:
                raise RuntimeError(
                    f"full_rollout_copy commit 缺少 item position: {commit_id_value}/{sequence}"
                )
            item_positions.append(new_positions[sequence])
        validate_commit_contract(
            commit_kind=commit_kind,
            commit_mode=commit_mode,
            item_count=len(item_positions),
            outcome=outcome,
        )
        subject_map = {
            "acceptance": "turn",
            "item_convergence": "item",
            "terminal_convergence": "turn",
            "assembly_sealed": "assembly",
        }[commit_kind]
        target_subject = (
            mapped(subject_map, subject_id) if subject_id is not None else subject_id
        )
        if subject_id is not None and target_subject is None:
            raise RuntimeError(
                f"full_rollout_copy storage commit subject mapping 缺失: {commit_id_value}"
            )
        target_idempotency_key = idempotency_key
        if commit_kind == "acceptance" and idempotency_key is not None:
            target_idempotency_key = mapped(
                "acceptance_idempotency_key", idempotency_key
            )
        remapped_metadata = remap_json(metadata_value)
        if not isinstance(remapped_metadata, Mapping):
            raise TypeError(
                f"full_rollout_copy storage commit metadata remap 必须是 object: {commit_id_value}"
            )
        if item_positions:
            start_offset = min(position[0] for position in item_positions)
            end_offset = max(position[0] + position[1] for position in item_positions)
            current_offset = end_offset
        else:
            start_offset = current_offset
            end_offset = current_offset
        result = connection.execute(
            "UPDATE storage_commits SET subject_id = ?, idempotency_key = ?, metadata_json = ?, jsonl_start_offset = ?, jsonl_end_offset = ?, jsonl_offset_before = ?, jsonl_offset_after = ?, jsonl_record_count = ? WHERE commit_id = ?",
            (
                target_subject,
                target_idempotency_key,
                _json(remapped_metadata),
                start_offset,
                end_offset,
                start_offset,
                end_offset,
                len(item_positions),
                commit_id_value,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"full_rollout_copy storage commit remap 行数不一致: {commit_id_value}"
            )
