"""target session 摘要在 bounded registry、snapshot 和 hash preimage 中同步。"""

from __future__ import annotations

import json

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
    """只同步 target-local detail 的精确完整性与可用性；正文 hash 不因 fork 改写。"""
    for key, record in state.detail_records.items():
        state.connection.execute(
            "UPDATE context_plan_details SET content_hash=?, status=?, availability=? "
            "WHERE detail_ref=? AND session_id=? AND thread_id=?",
            (
                record.content_hash, record.status, record.availability, key,
                state.target_session_id, state.target_thread_id,
            ),
        )
