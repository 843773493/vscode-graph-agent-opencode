"""冻结 schema3 manifest 合同；仅用于显式 2→3 中间态和 3→4 预检。"""

from __future__ import annotations

import sqlite3

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    optional_detail_ref_key,
)
from app.services.infrastructure.rollout_context.assembly.detail_registry import (
    validate_assembly_details,
)
from app.services.infrastructure.rollout_context.assembly.validation import (
    exact_row,
)


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


class Schema3AssemblyManifestValidator:
    """只验证 sealed assembly 的派生 SQLite manifest。"""

    def _validate_context_assembly_manifest(
        self,
        connection: sqlite3.Connection,
        snapshot: ContextAssemblySnapshot,
        *,
        header_detail_ref: str | None = None,
        checkpoint_ns: str = "",
    ) -> None:
        """校验 snapshot JSON 与 assembly manifest 表的逐字段一致性。

        snapshot_json 是 sealed view 的便携副本，manifest 表是 SQLite 的可检索
        索引；两者不是两个可选择的事实源。恢复时任何缺行、重复、legacy
        alias 分歧或 hash/ordinal 不一致都必须失败。
        """
        ref_rows = connection.execute(
            "SELECT ref_ordinal, ref_type, ref_id, semantic_kind, payload_kind, status, content_hash, source_revision, content_length, redacted_stable_digest, detail_ref, contribution_id, visibility, protection, availability FROM assembly_item_refs WHERE assembly_id = ? ORDER BY ref_ordinal",
            (snapshot.assembly_id,),
        ).fetchall()
        expected_refs = [
            (
                ordinal,
                ref.ref_type,
                ref.ref_id,
                ref.semantic_kind,
                ref.payload_kind,
                ref.status,
                ref.content_hash,
                ref.source_revision,
                ref.content_length,
                ref.redacted_stable_digest,
                optional_detail_ref_key(entry.detail_ref) if entry else None,
                entry.contribution_id if entry else None,
                ref.visibility,
                ref.protection,
                ref.availability,
            )
            for ordinal, ref in enumerate(snapshot.refs)
            for entry in [
                next(
                    (
                        candidate
                        for candidate in snapshot.selection
                        if (candidate.ref.ref_type, candidate.ref.ref_id)
                        == (ref.ref_type, ref.ref_id)
                    ),
                    None,
                )
            ]
        ]
        exact_row(
            tuple(ref_rows),
            tuple(expected_refs),
            field=f"source-mismatch: context assembly refs manifest: {snapshot.assembly_id}",
        )

        contribution_rows = connection.execute(
            "SELECT contribution_ordinal, contribution_id, source_kind, source_revision, content_hash, content_length, redacted_stable_digest, request_only, contribution_kind, visibility, protection, metadata_json FROM context_assembly_contributions WHERE assembly_id = ? ORDER BY contribution_ordinal",
            (snapshot.assembly_id,),
        ).fetchall()
        expected_contributions = []
        for contribution in sorted(
            snapshot.contributions, key=lambda item: item.contribution_ordinal,
        ):
            expected_ordinal = contribution.contribution_ordinal
            expected_contributions.append(
                (
                    expected_ordinal,
                    contribution.contribution_id,
                    contribution.source_kind,
                    contribution.source_revision,
                    contribution.content_hash,
                    contribution.content_length,
                    contribution.redacted_stable_digest,
                    int(contribution.request_only),
                    contribution.contribution_kind,
                    contribution.visibility,
                    contribution.protection,
                    _json(dict(contribution.metadata)),
                )
            )
        exact_row(
            tuple(contribution_rows),
            tuple(expected_contributions),
            field=f"source-mismatch: context assembly contribution manifest: {snapshot.assembly_id}",
        )
        selection_rows = connection.execute(
            "SELECT plan_ordinal, selection_kind, ref_type, ref_id, included, omission_reason, loss_json, source_revision, content_length, content_hash, redacted_stable_digest, visibility, protection, availability, base_delta_role, source_overlay_epoch, overlay_from_revision, overlay_to_revision, overlay_diff_hash, detail_ref, contribution_id, contribution_ordinal FROM context_assembly_selections WHERE assembly_id = ? ORDER BY plan_ordinal",
            (snapshot.assembly_id,),
        ).fetchall()
        expected_selection = [
            (
                entry.plan_ordinal,
                entry.selection_kind,
                entry.ref.ref_type,
                entry.ref.ref_id,
                int(entry.included),
                entry.omission_reason,
                _json(list(entry.loss)),
                entry.source_revision,
                entry.content_length,
                entry.content_hash,
                entry.redacted_stable_digest,
                entry.visibility,
                entry.protection,
                entry.availability,
                entry.base_delta_role,
                entry.source_overlay_epoch,
                entry.overlay_from_revision,
                entry.overlay_to_revision,
                entry.overlay_diff_hash,
                optional_detail_ref_key(entry.detail_ref),
                entry.contribution_id,
                entry.contribution_ordinal,
            )
            for entry in snapshot.selection
        ]
        exact_row(
            tuple(selection_rows),
            tuple(expected_selection),
            field=f"source-mismatch: context assembly selection manifest: {snapshot.assembly_id}",
        )

        tools = connection.execute(
            "SELECT tool_set_snapshot_id, session_id, plan_id, assembly_id, source_revision, "
            "tool_set_schema, tool_set_schema_version, tool_policy_version, content_length, "
            "content_hash, redacted_stable_digest, protection, availability, "
            "tool_policy_json, tools_json FROM tool_set_snapshots WHERE assembly_id = ?",
            (snapshot.assembly_id,),
        ).fetchall()
        expected_tools = {
            (ref.ref_id, snapshot.session_id, snapshot.plan_id, snapshot.assembly_id,
             ref.source_revision, ref.tool_set_schema, ref.tool_set_schema_version,
             ref.tool_policy_version, ref.content_length, ref.content_hash,
             ref.redacted_stable_digest, ref.protection, ref.availability,
             _json(dict(ref.tool_policy)), _json([dict(tool) for tool in ref.tools]))
            for ref in snapshot.tool_set_refs
        }
        if len(tools) != len(expected_tools) or set(tools) != expected_tools:
            raise ValueError("source-mismatch: schema3 ToolSetSnapshot manifest 不一致")
        validate_assembly_details(connection, snapshot, checkpoint_ns, header_detail_ref)


__all__ = ["Schema3AssemblyManifestValidator"]
