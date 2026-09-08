"""v2 cross-session fork identity/materialization owners。

所有复制操作只消费已提交 v2 storage state；source 坐标进入 lineage audit，
目标运行时只使用 target-local identity。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from app.services.infrastructure.rollout_context.fork.lineage.acceptance import (
    ForkAcceptanceMappingMixin,
)
from app.services.infrastructure.rollout_context.fork.lineage.legacy import (
    _legacy_source_identity_map,
    _legacy_source_item_offsets,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    required_text,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ForkIdentityMappingMixin(ForkAcceptanceMappingMixin):
    """复制实体的不可变跨 session lineage。"""

    def _record_copied_v2_identity_mappings(
        self,
        connection: sqlite3.Connection,
        *,
        source_session_id: str,
        target_session_id: str,
        fork_id: str,
        timestamp: str,
    ) -> None:
        """为 full copy 的 target-local 数据建立 source lineage 索引。

        full copy 的 SQLite/JSONL 已在 target 节点内复制完成；除 acceptance
        key 外，v2 local id 可以安全保留其字符串值，因为真正的全局身份是
        ``(session_id, entity_type, local_id)``。显式记录 identity mapping 使
        source offset、detail、assembly 和 operation anchor 的跨会话来源可查，
        也避免调用方把 target 的 local id 当成 source 裸引用。
        """
        source_session_id = required_text(source_session_id, field="source_session_id")
        target_session_id = required_text(target_session_id, field="target_session_id")
        fork_id = required_text(fork_id, field="fork_id")
        timestamp = required_text(timestamp, field="timestamp")
        mapped_entity_types = {
            "accepted_ingress",
            "acceptance_idempotency_key",
        }
        legacy_source_map = _legacy_source_identity_map(
            connection,
            source_session_id=source_session_id,
            target_session_id=target_session_id,
        )
        legacy_source_offsets = _legacy_source_item_offsets(
            connection,
            source_session_id=source_session_id,
            target_session_id=target_session_id,
        )

        def add_mapping(
            entity_type: str,
            local_id: str,
            *,
            source_offset: int | None = None,
            target_offset: int | None = None,
            ordinal: int | None = None,
            lineage_extra: Mapping[str, object] | None = None,
        ) -> None:
            if entity_type in mapped_entity_types:
                return
            local_id = required_text(local_id, field=f"{entity_type}.local_id")
            source_local_id = legacy_source_map.get(entity_type, {}).get(
                local_id, local_id
            )
            source_local_id = required_text(
                source_local_id, field=f"{entity_type}.source_local_id"
            )
            source_offset = strict_optional_non_negative_int(
                source_offset, field=f"{entity_type}.source_offset"
            )
            target_offset = strict_optional_non_negative_int(
                target_offset, field=f"{entity_type}.target_offset"
            )
            ordinal = strict_optional_non_negative_int(
                ordinal, field=f"{entity_type}.ordinal"
            )
            is_legacy_source = source_local_id != local_id
            existing = connection.execute(
                "SELECT target_local_id FROM fork_identity_mappings WHERE fork_id = ? AND entity_type = ? AND source_local_id = ?",
                (fork_id, entity_type, source_local_id),
            ).fetchone()
            if existing is not None:
                if (
                    strict_text(
                        existing[0], field="fork_identity_mappings.target_local_id"
                    )
                    != local_id
                ):
                    raise ValueError(
                        f"fork {entity_type} identity mapping 冲突: {source_local_id}"
                    )
                return
            target_existing = connection.execute(
                "SELECT source_local_id FROM fork_identity_mappings WHERE fork_id = ? AND entity_type = ? AND target_local_id = ?",
                (fork_id, entity_type, local_id),
            ).fetchone()
            if target_existing is not None:
                target_source_local_id = strict_text(
                    target_existing[0],
                    field="fork_identity_mappings.source_local_id",
                )
                if target_source_local_id != source_local_id:
                    raise ValueError(
                        f"fork {entity_type} target identity 冲突: {local_id}"
                    )
            if ordinal is None:
                ordinal_specs = {
                    "turn": ("turn_records", "turn_id", "turn_ordinal"),
                    "execution": (
                        "executions",
                        "execution_id",
                        "execution_ordinal",
                    ),
                    "model_call": (
                        "model_calls",
                        "model_call_id",
                        "attempt_ordinal",
                    ),
                }
                spec = ordinal_specs.get(entity_type)
                if spec is not None:
                    table, id_column, ordinal_column = spec
                    row = connection.execute(
                        f"SELECT {ordinal_column} FROM {table} WHERE {id_column} = ?",
                        (local_id,),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError(
                            f"fork {entity_type} target row 缺失: {local_id}"
                        )
                    ordinal = strict_non_negative_int(
                        row[0], field=f"{table}.{ordinal_column}:{local_id}"
                    )
            lineage = {
                "identity_mode": (
                    "legacy_migrated_target_local"
                    if is_legacy_source
                    else "preserved_target_local"
                ),
                "source": {
                    "session_id": source_session_id,
                    "local_id": source_local_id,
                    "offset": source_offset,
                    "ordinal": ordinal,
                },
                "target": {
                    "session_id": target_session_id,
                    "local_id": local_id,
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
                    source_local_id,
                    local_id,
                    source_offset,
                    target_offset,
                    _json(lineage),
                    timestamp,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"fork {entity_type} identity mapping 写入失败: {local_id}"
                )

        def item_offset(item_id: str) -> int | None:
            row = connection.execute(
                "SELECT jsonl_offset FROM item_catalog WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            return (
                strict_non_negative_int(
                    row[0], field=f"item_catalog.jsonl_offset:{item_id}"
                )
                if row is not None
                else None
            )

        def source_item_offset(item_id: str, target_offset: int | None) -> int | None:
            return legacy_source_offsets.get(item_id, target_offset)

        for item_id, offset, sequence in connection.execute(
            "SELECT item_id, jsonl_offset, item_sequence FROM item_catalog ORDER BY item_sequence"
        ).fetchall():
            item_id = strict_text(item_id, field="item_catalog.item_id")
            offset = strict_non_negative_int(
                offset, field=f"item_catalog.jsonl_offset:{item_id}"
            )
            sequence = strict_non_negative_int(
                sequence, field=f"item_catalog.item_sequence:{item_id}"
            )
            if sequence == 0 or offset < 0:
                raise RuntimeError(f"fork item locator 非法: {item_id}")
            add_mapping(
                "item",
                item_id,
                source_offset=source_item_offset(item_id, offset),
                target_offset=offset,
                ordinal=sequence,
            )
        for entity_type, table, column in (
            ("turn", "turn_records", "turn_id"),
            ("execution", "executions", "execution_id"),
            ("model_call", "model_calls", "model_call_id"),
            ("assembly", "context_assemblies", "assembly_id"),
            ("view", "context_views", "view_id"),
            ("branch", "branches", "branch_id"),
            ("checkpoint", "checkpoints", "checkpoint_id"),
            ("anchor", "operation_anchors", "anchor_id"),
            ("relation", "item_relations", "relation_id"),
            ("context_contribution", "context_contributions", "contribution_id"),
        ):
            for (local_id,) in connection.execute(
                f"SELECT {column} FROM {table} ORDER BY {column}"
            ).fetchall():
                add_mapping(
                    entity_type, strict_text(local_id, field=f"{table}.{column}")
                )
        for (local_id,) in connection.execute(
            "SELECT tool_set_snapshot_id FROM tool_set_snapshots ORDER BY tool_set_snapshot_id"
        ).fetchall():
            add_mapping(
                "tool_set",
                strict_text(local_id, field="tool_set_snapshots.tool_set_snapshot_id"),
            )
        for overlay_id, epoch, base_ref, delta_ref in connection.execute(
            "SELECT overlay_id, source_overlay_epoch, base_ref, delta_ref FROM source_overlays ORDER BY overlay_id"
        ).fetchall():
            overlay_id = strict_text(overlay_id, field="source_overlays.overlay_id")
            epoch = strict_non_negative_int(
                epoch, field=f"source_overlays.source_overlay_epoch:{overlay_id}"
            )
            base_ref = strict_optional_text(
                base_ref, field=f"source_overlays.base_ref:{overlay_id}"
            )
            delta_ref = strict_optional_text(
                delta_ref, field=f"source_overlays.delta_ref:{overlay_id}"
            )
            add_mapping(
                "source_overlay",
                overlay_id,
                lineage_extra={
                    "source_overlay_epoch": epoch,
                    "target_overlay_epoch": epoch,
                    "source_base_ref": base_ref,
                    "source_delta_ref": delta_ref,
                    "target_base_ref": base_ref,
                    "target_delta_ref": delta_ref,
                    "localization": "target_session_node",
                },
            )
        for detail_ref, relative_path in connection.execute(
            "SELECT detail_ref, relative_path FROM context_plan_details ORDER BY detail_ref"
        ).fetchall():
            detail_ref = strict_text(
                detail_ref, field="context_plan_details.detail_ref"
            )
            relative_path = strict_text(
                relative_path,
                field=f"context_plan_details.relative_path:{detail_ref}",
            )
            add_mapping(
                "detail",
                detail_ref,
                lineage_extra={
                    "source_relative_path": relative_path,
                    "target_relative_path": relative_path,
                    "localization": "target_session_node",
                },
            )
        for tool_call_id, assistant_sequence in connection.execute(
            "SELECT tool_call_id, assistant_message_sequence FROM tool_calls ORDER BY assistant_message_sequence, tool_call_id"
        ).fetchall():
            # tool_call_id 是 payload 中 provider block 的实际 identity；
            # assistant_message_sequence 只是旧 SQLite locator，不能把复合
            # locator 写进 source->target mapping 后又让 JSONL payload 保留
            # source tool_call_id。
            tool_call_id = strict_text(tool_call_id, field="tool_calls.tool_call_id")
            assistant_sequence = strict_non_negative_int(
                assistant_sequence,
                field=f"tool_calls.assistant_message_sequence:{tool_call_id}",
            )
            add_mapping("tool_call", tool_call_id)
            add_mapping(
                "tool_call_locator",
                f"{tool_call_id}:{assistant_sequence}",
            )
        for turn_id, execution_id in connection.execute(
            "SELECT turn_id, execution_id FROM turn_execution_links ORDER BY turn_id, execution_ordinal"
        ).fetchall():
            turn_id = strict_text(turn_id, field="turn_execution_links.turn_id")
            execution_id = strict_text(
                execution_id, field="turn_execution_links.execution_id"
            )
            add_mapping("execution_link", f"{turn_id}:{execution_id}")
        for view_id, turn_id, logical_ordinal in connection.execute(
            "SELECT view_id, turn_id, logical_turn_ordinal FROM context_view_turns ORDER BY view_id, logical_turn_ordinal"
        ).fetchall():
            view_id = strict_text(view_id, field="context_view_turns.view_id")
            turn_id = strict_text(turn_id, field="context_view_turns.turn_id")
            logical_ordinal = strict_non_negative_int(
                logical_ordinal,
                field=f"context_view_turns.logical_turn_ordinal:{view_id}",
            )
            add_mapping(
                "view_turn",
                f"{view_id}:{turn_id}",
                ordinal=logical_ordinal,
            )
        for view_id, item_id, logical_ordinal in connection.execute(
            "SELECT view_id, item_id, logical_item_ordinal FROM context_view_items ORDER BY view_id, logical_item_ordinal"
        ).fetchall():
            view_id = strict_text(view_id, field="context_view_items.view_id")
            item_id = strict_text(item_id, field="context_view_items.item_id")
            logical_ordinal = strict_non_negative_int(
                logical_ordinal,
                field=f"context_view_items.logical_item_ordinal:{view_id}",
            )
            offset = item_offset(item_id)
            add_mapping(
                "view_item",
                f"{view_id}:{item_id}",
                source_offset=source_item_offset(item_id, offset),
                target_offset=offset,
                ordinal=logical_ordinal,
            )
        for item_id, part_id, part_ordinal in connection.execute(
            "SELECT item_id, part_id, part_ordinal FROM item_parts ORDER BY item_id, part_ordinal"
        ).fetchall():
            item_id = strict_text(item_id, field="item_parts.item_id")
            part_id = strict_text(part_id, field="item_parts.part_id")
            part_ordinal = strict_non_negative_int(
                part_ordinal, field=f"item_parts.part_ordinal:{item_id}/{part_id}"
            )
            offset = item_offset(item_id)
            if offset is None:
                raise RuntimeError(
                    f"fork item part parent item 缺失: {item_id}/{part_id}"
                )
            add_mapping(
                "content_part",
                f"{item_id}:{part_id}",
                source_offset=source_item_offset(item_id, offset),
                target_offset=offset,
                ordinal=part_ordinal,
            )

    def _full_copy_identity(
        self,
        fork_id: str,
        target_session_id: str,
        entity_type: str,
        source_local_id: str,
    ) -> str:
        digest = hashlib.sha256(
            f"{fork_id}:{entity_type}:{source_local_id}".encode()
        ).hexdigest()[:32]
        return f"fork-{entity_type}:{target_session_id}:{digest}"
