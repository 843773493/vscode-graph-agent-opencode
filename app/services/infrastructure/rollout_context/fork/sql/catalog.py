"""full_rollout_copy 的 SQLite identity/reference 收敛。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)
from app.services.infrastructure.rollout_context.fork.sql.common import (
    _json,
    rewrite_column,
    rewrite_reference_column,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)


def rewrite_full_copy_fast_columns(state: FullCopyRemapState) -> None:
    connection = state.connection
    target_session_id = state.target_session_id
    maps = state.maps
    item_rows = state.item_rows
    part_maps = state.part_maps
    new_items = state.new_items
    mapped = state.mapped
    remap_json = state.remap_json
    item_by_source_id = {
        required_text(row[1], field="item_catalog.item_id"): new_items[
            non_negative_int(row[0], field="item_catalog.item_sequence")
        ]
        for row in item_rows
    }
    for old_id, new_id in maps.get("item", {}).items():
        item = item_by_source_id.get(old_id)
        if item is None:
            raise RuntimeError(f"full_rollout_copy item manifest 丢失: {old_id}")
        old_row = connection.execute(
            "SELECT turn_id FROM item_catalog WHERE item_id = ?", (old_id,)
        ).fetchone()
        if old_row is None:
            raise RuntimeError(f"full_rollout_copy item catalog 丢失: {old_id}")
        old_turn_id = optional_text(old_row[0], field="item_catalog.turn_id")
        result = connection.execute(
            "UPDATE item_catalog SET item_id = ?, turn_id = ?, producer_ref_json = ?, content_hash = ?, metadata_json = ? WHERE item_id = ?",
            (
                new_id,
                mapped("turn", old_turn_id),
                _json(item.producer_ref),
                item.content_hash,
                _json(item.metadata),
                old_id,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(f"full_rollout_copy item catalog remap 失败: {old_id}")

    for old_id, new_id in maps.get("item", {}).items():
        for table, column in (
            ("item_projections", "item_id"),
            ("item_projections", "id"),
            ("context_view_items", "item_id"),
            ("item_parts", "item_id"),
            ("operation_anchors", "item_id"),
        ):
            rewrite_column(connection, table, column, old_id, new_id)

    for old_id, new_id in maps.get("turn", {}).items():
        for table, column in (
            ("messages", "turn_id"),
            ("item_projections", "turn_id"),
            ("turns", "turn_id"),
            ("turn_records", "turn_id"),
            ("context_view_turns", "turn_id"),
            ("context_assemblies", "turn_id"),
        ):
            rewrite_column(connection, table, column, old_id, new_id)

    for old_id, new_id in maps.get("execution", {}).items():
        for table, column in (
            ("executions", "execution_id"),
            ("model_calls", "execution_id"),
            ("turn_execution_links", "execution_id"),
            ("context_assemblies", "execution_id"),
        ):
            rewrite_column(connection, table, column, old_id, new_id)

    for entity_type, table, column in (
        ("model_call", "model_calls", "model_call_id"),
        ("view", "context_views", "view_id"),
        ("branch", "branches", "branch_id"),
        ("checkpoint", "checkpoints", "checkpoint_id"),
        ("anchor", "operation_anchors", "anchor_id"),
        ("relation", "item_relations", "relation_id"),
        ("context_contribution", "context_contributions", "contribution_id"),
    ):
        for old_id, new_id in maps.get(entity_type, {}).items():
            rewrite_column(connection, table, column, old_id, new_id)

    for old_id, new_id in maps.get("branch", {}).items():
        for table, column in (
            ("context_views", "branch_id"),
            ("context_view_turns", "branch_id"),
            ("checkpoints", "branch_id"),
            ("checkpoint_namespace_state", "active_branch_id"),
        ):
            if column in {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }:
                rewrite_column(connection, table, column, old_id, new_id)

    for old_id, new_id in maps.get("view", {}).items():
        for table, column in (
            ("context_view_ranges", "view_id"),
            ("context_view_ranges", "source_view_id"),
            ("context_view_jumps", "view_id"),
            ("context_view_jumps", "ancestor_view_id"),
            ("context_view_turns", "view_id"),
            ("checkpoints", "view_id"),
            ("checkpoint_channels", "context_view_id"),
            ("operation_anchors", "view_id"),
        ):
            rewrite_column(connection, table, column, old_id, new_id)

    for old_id, new_id in maps.get("checkpoint", {}).items():
        for table, column in (
            ("checkpoints", "checkpoint_id"),
            ("checkpoints", "parent_checkpoint_id"),
            ("checkpoint_channels", "checkpoint_id"),
            ("pending_writes", "checkpoint_id"),
        ):
            rewrite_column(connection, table, column, old_id, new_id)

    for old_id, new_id in maps.get("message", {}).items():
        rewrite_column(connection, "messages", "message_id", old_id, new_id)

    for old_id, new_id in maps.get("tool_call", {}).items():
        rewrite_column(connection, "tool_calls", "tool_call_id", old_id, new_id)

    for (old_item_id, old_part_id), new_part_id in part_maps.items():
        target_item_id = mapped("item", old_item_id)
        if target_item_id is None:
            raise RuntimeError(
                f"full_rollout_copy item part 缺少 target item: {old_item_id}"
            )
        count_row = connection.execute(
            "SELECT COUNT(*) FROM item_parts WHERE item_id = ? AND part_id = ?",
            (target_item_id, old_part_id),
        ).fetchone()
        if count_row is None:
            raise RuntimeError("full_rollout_copy item part 行数读取失败")
        expected = non_negative_int(count_row[0], field="item_parts.match_count")
        if expected != 1:
            raise RuntimeError(
                "full_rollout_copy item part manifest 缺失或重复: "
                f"{target_item_id}/{old_part_id}"
            )
        result = connection.execute(
            "UPDATE item_parts SET part_id = ? WHERE item_id = ? AND part_id = ?",
            (new_part_id, target_item_id, old_part_id),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"full_rollout_copy item part remap 失败: {target_item_id}/{old_part_id}"
            )
        anchor_count_row = connection.execute(
            "SELECT COUNT(*) FROM operation_anchors WHERE item_id = ? AND part_id = ?",
            (target_item_id, old_part_id),
        ).fetchone()
        if anchor_count_row is None:
            raise RuntimeError("full_rollout_copy operation anchor 行数读取失败")
        anchor_count = non_negative_int(
            anchor_count_row[0], field="operation_anchors.match_count"
        )
        if anchor_count:
            result = connection.execute(
                "UPDATE operation_anchors SET part_id = ? WHERE item_id = ? AND part_id = ?",
                (new_part_id, target_item_id, old_part_id),
            )
            if result.rowcount != anchor_count:
                raise RuntimeError(
                    "full_rollout_copy operation anchor part remap 行数不一致: "
                    f"{target_item_id}/{old_part_id}"
                )

    for old_id, new_id in maps.get("detail", {}).items():
        for table in ("assembly_item_refs", "context_assembly_selections"):
            rewrite_column(connection, table, "detail_ref", old_id, new_id)
        target_detail = detail_ref_from_key(new_id)
        target_detail.require_owner(target_session_id)
        result = connection.execute(
            "UPDATE context_plan_details SET detail_ref = ?, session_id = ?, assembly_id = ?, detail_id = ?, relative_path = ? WHERE detail_ref = ?",
            (
                new_id,
                target_detail.session_id,
                target_detail.assembly_id,
                target_detail.detail_id,
                detail_relative_path(target_detail).as_posix(),
                old_id,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(f"full_rollout_copy detail remap 失败: {old_id}")
        if new_id in state.detail_hashes:
            connection.execute(
                "UPDATE context_plan_details SET content_hash=? WHERE detail_ref=?",
                (state.detail_hashes[new_id], new_id),
            )

    for old_id, new_id in maps.get("source_overlay", {}).items():
        old_overlay = connection.execute(
            "SELECT supersedes_overlay_id, materializes_overlay_id, base_ref, delta_ref FROM source_overlays WHERE overlay_id = ?",
            (old_id,),
        ).fetchone()
        if old_overlay is None:
            raise RuntimeError(
                f"full_rollout_copy source overlay manifest 丢失: {old_id}"
            )
        result = connection.execute(
            "UPDATE source_overlays SET overlay_id = ?, supersedes_overlay_id = ?, materializes_overlay_id = ?, base_ref = ?, delta_ref = ? WHERE overlay_id = ?",
            (
                new_id,
                mapped(
                    "source_overlay",
                    optional_text(
                        old_overlay[0],
                        field="source_overlays.supersedes_overlay_id",
                    ),
                ),
                mapped(
                    "source_overlay",
                    optional_text(
                        old_overlay[1],
                        field="source_overlays.materializes_overlay_id",
                    ),
                ),
                mapped(
                    "request_ref",
                    optional_text(old_overlay[2], field="source_overlays.base_ref"),
                ),
                mapped(
                    "request_ref",
                    optional_text(old_overlay[3], field="source_overlays.delta_ref"),
                ),
                old_id,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(f"full_rollout_copy source overlay remap 失败: {old_id}")

    for old_id, new_id in maps.get("request_ref", {}).items():
        for table, column in (
            ("assembly_item_refs", "ref_id"),
            ("context_assembly_selections", "ref_id"),
        ):
            rewrite_reference_column(
                connection, table, column, "request_only", old_id, new_id
            )

    for old_id, new_id in maps.get("tool_set", {}).items():
        rewrite_reference_column(
            connection,
            "context_assembly_selections",
            "ref_id",
            "tool_set",
            old_id,
            new_id,
        )

    assembly_rows = connection.execute(
        "SELECT assembly_id, plan_id, detail_ref, snapshot_json FROM context_assemblies"
    ).fetchall()

    for (
        old_assembly_id,
        old_plan_id,
        old_detail_ref,
        snapshot_json,
    ) in assembly_rows:
        old_assembly_id = required_text(
            old_assembly_id, field="context_assemblies.assembly_id"
        )
        old_plan_id = required_text(old_plan_id, field="context_assemblies.plan_id")
        old_detail_ref = optional_text(
            old_detail_ref, field="context_assemblies.detail_ref"
        )
        snapshot_text = required_text(
            snapshot_json, field="context_assemblies.snapshot_json"
        )
        value = json.loads(snapshot_text)
        if not isinstance(value, Mapping):
            raise TypeError(
                f"full_rollout_copy assembly snapshot 非 object: {old_assembly_id}"
            )
        remapped_value = remap_json(value)
        if not isinstance(remapped_value, dict):
            raise TypeError("full_rollout_copy assembly snapshot remap 非 object")
        remapped_value["session_id"] = target_session_id
        result = connection.execute(
            "UPDATE context_assemblies SET plan_id = ?, detail_ref = ?, snapshot_json = ? WHERE assembly_id = ?",
            (
                mapped("plan", old_plan_id),
                mapped("detail", old_detail_ref),
                _json(remapped_value),
                # 此时 assembly 主键还未由后面的跨表重写阶段更新，
                # 必须用 source/local key 定位本行；使用 target key
                # 会静默更新 0 行并留下 source-owned snapshot。
                old_assembly_id,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"full_rollout_copy context assembly snapshot remap 失败: {old_assembly_id}"
            )

    for old_assembly_id, new_assembly_id in maps.get("assembly", {}).items():
        for table in (
            "assembly_item_refs",
            "context_assembly_contributions",
            "context_assembly_selections",
            "context_contributions",
        ):
            rewrite_column(
                connection, table, "assembly_id", old_assembly_id, new_assembly_id
            )

    for old_id, new_id in maps.get("context_contribution", {}).items():
        for table in (
            "context_assembly_contributions",
            "assembly_item_refs",
            "context_assembly_selections",
        ):
            rewrite_column(connection, table, "contribution_id", old_id, new_id)

    # contribution manifest 的 metadata 也属于 durable ref graph。只改主键
    # 会留下 source overlay_id/source_ref，重启后 active overlay 与
    # contribution 的 role/epoch 校验会把合法正文判成孤儿。
    contribution_rows = [
        (table, *row)
        for table in ("context_contributions", "context_assembly_contributions")
        for row in connection.execute(
            f"SELECT assembly_id, contribution_id, metadata_json FROM {table}"
        ).fetchall()
    ]
    for table, assembly_id, contribution_id, metadata_json in contribution_rows:
        contribution_id = required_text(
            contribution_id, field="context_contributions.contribution_id"
        )
        metadata_text = required_text(
            metadata_json, field="context_contributions.metadata_json"
        )
        value = json.loads(metadata_text)
        if not isinstance(value, Mapping):
            raise TypeError(
                "full_rollout_copy context contribution metadata 必须是 object: "
                f"{contribution_id}"
            )
        remapped = remap_json(value)
        if not isinstance(remapped, dict):
            raise TypeError(
                "full_rollout_copy context contribution metadata remap 非 object"
            )
        result = connection.execute(
            f"UPDATE {table} SET metadata_json = ? WHERE contribution_id = ? AND assembly_id IS ?",
            (_json(remapped), contribution_id, assembly_id),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"full_rollout_copy contribution metadata remap 失败: {contribution_id}"
            )
