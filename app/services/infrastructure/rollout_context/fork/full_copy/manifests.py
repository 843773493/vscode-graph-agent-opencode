"""target session 摘要在 bounded registry、snapshot 和 hash preimage 中同步。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)


def record_source_fingerprints(state: FullCopyRemapState) -> None:
    """target 的新 hash 不覆盖 source hash，原 fingerprint 只进入不可运行 lineage。"""
    for assembly_id, (
        plan_id,
        plan_hash,
        request_hash,
    ) in state.source_assembly_fingerprints.items():
        key = (state.fork_id, "assembly", assembly_id)
        row = state.connection.execute(
            "SELECT lineage_json FROM fork_identity_mappings WHERE fork_id=? AND entity_type=? AND source_local_id=?",
            key,
        ).fetchone()
        if row is None:
            raise RuntimeError("source-mismatch: fork assembly lineage 缺失")
        lineage = json.loads(row[0])
        lineage["source_assembly"] = {
            "session_id": state.source_session_id,
            "assembly_id": assembly_id,
            "plan_id": plan_id,
            "plan_hash": plan_hash,
            "request_hash": request_hash,
        }
        state.connection.execute(
            "UPDATE fork_identity_mappings SET lineage_json=? WHERE fork_id=? AND entity_type=? AND source_local_id=?",
            (canonical_json_bytes(lineage).decode("utf-8"), *key),
        )


def refresh_detail_manifests(state: FullCopyRemapState) -> None:
    connection = state.connection
    for key, record in state.detail_records.items():
        connection.execute(
            "UPDATE context_plan_details SET content_hash=?, redacted_stable_digest=?, status=?, availability=? WHERE detail_ref=?",
            (
                record.content_hash,
                record.redacted_stable_digest,
                record.status,
                record.availability,
                key,
            ),
        )
    digest_columns = (
        ("assembly_item_refs", "redacted_stable_digest"),
        ("context_assembly_selections", "redacted_stable_digest"),
        ("context_contributions", "redacted_stable_digest"),
        ("context_assembly_contributions", "redacted_stable_digest"),
        ("source_overlays", "base_redacted_stable_digest"),
        ("source_overlays", "delta_redacted_stable_digest"),
    )
    for source, target in state.detail_digests.items():
        for table, column in digest_columns:
            connection.execute(
                f"UPDATE {table} SET {column}=? WHERE {column}=?", (target, source)
            )

    def refresh(value):
        if isinstance(value, list):
            return [refresh(child) for child in value]
        if not isinstance(value, Mapping):
            return value
        result = {}
        for key, child in value.items():
            # 只改正式摘要字段；用户文本和 source lineage 中的字符串不作替换。
            if key in {"source", "lineage", "legacy_source_ref"}:
                result[key] = child
            elif key in {
                "redacted_stable_digest",
                "base_redacted_stable_digest",
                "delta_redacted_stable_digest",
            }:
                result[key] = state.detail_digests.get(child, child)
            else:
                result[key] = refresh(child)
        return result

    for table, key_column, json_column in (
        ("context_assemblies", "assembly_id", "snapshot_json"),
        ("context_contributions", "contribution_id", "metadata_json"),
    ):
        for key, raw in connection.execute(
            f"SELECT {key_column}, {json_column} FROM {table}"
        ).fetchall():
            encoded = canonical_json_bytes(refresh(json.loads(raw))).decode("utf-8")
            connection.execute(
                f"UPDATE {table} SET {json_column}=? WHERE {key_column}=?",
                (encoded, key),
            )
    for assembly, ordinal, raw in connection.execute(
        "SELECT assembly_id, contribution_ordinal, metadata_json FROM context_assembly_contributions"
    ).fetchall():
        connection.execute(
            "UPDATE context_assembly_contributions SET metadata_json=? WHERE assembly_id=? AND contribution_ordinal=?",
            (
                canonical_json_bytes(refresh(json.loads(raw))).decode("utf-8"),
                assembly,
                ordinal,
            ),
        )
