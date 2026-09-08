"""full_rollout_copy 的 SQLite identity/reference 收敛。"""

from __future__ import annotations

from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
)


def rewrite_full_copy_reference_columns(state: FullCopyRemapState) -> None:
    connection = state.connection
    maps = state.maps
    overlay_epoch_map = state.overlay_epoch_map
    table_columns: dict[str, set[str]] = {}

    def has_column(table: str, column: str) -> bool:
        if table not in table_columns:
            table_columns[table] = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
        return column in table_columns[table]

    def rewrite_column(table: str, column: str, old: object, new: object) -> None:
        if old is None or not has_column(table, column):
            return
        count_row = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (old,)
        ).fetchone()
        if count_row is None:
            raise RuntimeError(f"无法读取 remap 行数: {table}.{column}")
        expected = non_negative_int(count_row[0], field=f"{table}.{column}.match_count")
        result = connection.execute(
            f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
            (new, old),
        )
        if result.rowcount != expected:
            raise RuntimeError(
                f"full_rollout_copy reference remap 行数不一致: {table}.{column}, "
                f"expected={expected}, actual={result.rowcount}"
            )

    for old_id, new_id in maps.get("item", {}).items():
        for table, column in (
            ("item_projections", "item_id"),
            ("item_projections", "id"),
            ("item_catalog", "item_id"),
            ("context_view_items", "item_id"),
            ("item_parts", "item_id"),
            ("operation_anchors", "item_id"),
            ("context_view_turns", "root_input_item_id"),
            ("turn_records", "root_input_item_id"),
            ("turn_records", "final_item_id"),
            ("item_relations", "source_ref"),
            ("item_relations", "target_ref"),
        ):
            rewrite_column(table, column, old_id, new_id)
        for table in ("assembly_item_refs", "context_assembly_selections"):
            rewrite_column(table, "ref_id", old_id, new_id)

    for old_id, new_id in maps.get("turn", {}).items():
        for table, column in (
            ("messages", "turn_id"),
            ("item_catalog", "turn_id"),
            ("item_projections", "turn_id"),
            ("turns", "turn_id"),
            ("turn_records", "turn_id"),
            ("executions", "turn_id"),
            ("context_view_turns", "turn_id"),
            ("context_assemblies", "turn_id"),
            ("turn_acceptances", "turn_id"),
            ("turn_execution_links", "turn_id"),
        ):
            rewrite_column(table, column, old_id, new_id)
        rewrite_column("turn_records", "replay_of_turn_id", old_id, new_id)

    for old_id, new_id in maps.get("execution", {}).items():
        for table, column in (
            ("executions", "execution_id"),
            ("executions", "resumed_from_execution_id"),
            ("executions", "replay_of_execution_id"),
            ("executions", "initial_execution_id"),
            ("executions", "last_execution_id"),
            ("model_calls", "execution_id"),
            ("turn_execution_links", "execution_id"),
            ("context_assemblies", "execution_id"),
            ("turn_records", "initial_execution_id"),
            ("turn_records", "last_execution_id"),
        ):
            rewrite_column(table, column, old_id, new_id)

    for old_id, new_id in maps.get("model_call", {}).items():
        for table, column in (
            ("model_calls", "model_call_id"),
            ("model_calls", "retry_of_model_call_id"),
            ("context_assemblies", "model_call_id"),
        ):
            rewrite_column(table, column, old_id, new_id)

    for old_id, new_id in maps.get("assembly", {}).items():
        for table, column in (
            ("context_assemblies", "assembly_id"),
            ("assembly_item_refs", "assembly_id"),
            ("context_assembly_contributions", "assembly_id"),
            ("context_assembly_selections", "assembly_id"),
            ("context_contributions", "assembly_id"),
            ("model_calls", "assembly_id"),
        ):
            rewrite_column(table, column, old_id, new_id)

    for old_id, new_id in maps.get("view", {}).items():
        for table, column in (
            ("context_views", "view_id"),
            ("context_views", "parent_view_id"),
            ("context_views", "head_turn_id"),
            ("context_view_ranges", "view_id"),
            ("context_view_ranges", "source_view_id"),
            ("context_view_jumps", "view_id"),
            ("context_view_jumps", "ancestor_view_id"),
            ("context_view_turns", "view_id"),
            ("checkpoints", "view_id"),
            ("checkpoint_channels", "context_view_id"),
            ("operation_anchors", "view_id"),
            ("branches", "head_view_id"),
        ):
            rewrite_column(table, column, old_id, new_id)

    for old_id, new_id in maps.get("branch", {}).items():
        for table, column in (
            ("branches", "branch_id"),
            ("branches", "parent_branch_id"),
            ("context_views", "branch_id"),
            ("context_view_turns", "branch_id"),
            ("checkpoints", "branch_id"),
            ("checkpoint_namespace_state", "active_branch_id"),
            ("operation_anchors", "branch_id"),
            ("control_events", "branch_id"),
        ):
            rewrite_column(table, column, old_id, new_id)

    for old_id, new_id in maps.get("checkpoint", {}).items():
        for table, column in (
            ("checkpoints", "checkpoint_id"),
            ("checkpoints", "parent_checkpoint_id"),
            ("checkpoint_channels", "checkpoint_id"),
            ("pending_writes", "checkpoint_id"),
            ("control_events", "checkpoint_id"),
            ("branches", "head_checkpoint_id"),
        ):
            rewrite_column(table, column, old_id, new_id)

    for old_id, new_id in maps.get("message", {}).items():
        rewrite_column("messages", "message_id", old_id, new_id)
        rewrite_column("turns", "final_message_id", old_id, new_id)

    for old_id, new_id in maps.get("tool_call", {}).items():
        rewrite_column("tool_calls", "tool_call_id", old_id, new_id)

    for old_id, new_id in maps.get("context_contribution", {}).items():
        rewrite_column("context_contributions", "contribution_id", old_id, new_id)
        rewrite_column(
            "context_assembly_contributions", "contribution_id", old_id, new_id
        )

    for old_id, new_id in maps.get("source_overlay", {}).items():
        for column in (
            "overlay_id",
            "supersedes_overlay_id",
            "materializes_overlay_id",
        ):
            rewrite_column("source_overlays", column, old_id, new_id)

    for source_epoch, target_epoch in overlay_epoch_map.items():
        count_row = connection.execute(
            "SELECT COUNT(*) FROM source_overlays WHERE source_overlay_epoch = ?",
            (source_epoch,),
        ).fetchone()
        if count_row is None:
            raise RuntimeError("full_rollout_copy source overlay epoch 行数读取失败")
        expected = non_negative_int(
            count_row[0], field="source_overlays.source_overlay_epoch.match_count"
        )
        result = connection.execute(
            "UPDATE source_overlays SET source_overlay_epoch = ? WHERE source_overlay_epoch = ?",
            (target_epoch, source_epoch),
        )
        if result.rowcount != expected:
            raise RuntimeError(
                "full_rollout_copy source overlay epoch remap 行数不一致"
            )

    for old_id, new_id in maps.get("request_ref", {}).items():
        for table, column in (
            ("assembly_item_refs", "ref_id"),
            ("context_assembly_selections", "ref_id"),
            ("source_overlays", "base_ref"),
            ("source_overlays", "delta_ref"),
        ):
            rewrite_column(table, column, old_id, new_id)

    for old_id, new_id in maps.get("tool_set", {}).items():
        rewrite_column("context_assembly_selections", "ref_id", old_id, new_id)
        rewrite_column("tool_set_snapshots", "tool_set_snapshot_id", old_id, new_id)
