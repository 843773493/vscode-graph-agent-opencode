"""fork 的 sealed source 验证与 target plan/manifest/hash 本地化。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import payload_content_length
from app.domain.itemized.request_hash import context_request_hash
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    read_registration,
)
from app.services.infrastructure.rollout_context.fork.full_copy.plan_values import (
    localize_plan_value,
)
from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)


def validate_source_assemblies(
    service, connection: sqlite3.Connection, checkpoint_ns: str
) -> None:
    """复制前只读验证 source，不能重建 target manifest 掩盖原件损坏。"""
    for session_id, plan_id in connection.execute(
        "SELECT session_id, plan_id FROM context_plans"
    ).fetchall():
        read_registration(connection, session_id, plan_id)
    for raw, detail_ref in connection.execute(
        "SELECT snapshot_json, detail_ref FROM context_assemblies"
    ).fetchall():
        snapshot = ContextAssemblySnapshot.from_dict(json.loads(raw))
        service._validate_context_assembly_manifest(
            connection,
            snapshot,
            header_detail_ref=detail_ref,
            checkpoint_ns=checkpoint_ns,
        )
        snapshot.validate_hashes()


def refresh_copied_assemblies(state: FullCopyRemapState) -> None:
    """引用身份变更后按 domain 算法生成 target 自己的 fingerprint。"""
    connection = state.connection
    items = {item.item_id: item for item in state.new_items.values()}

    for item in items.values():
        for table in ("assembly_item_refs", "context_assembly_selections"):
            connection.execute(
                f"UPDATE {table} SET "
                "source_revision=CASE WHEN source_revision IS NULL THEN NULL ELSE ? END, "
                "content_hash=CASE WHEN content_hash IS NULL THEN NULL ELSE ? END, "
                "content_length=CASE WHEN content_length IS NULL THEN NULL ELSE ? END "
                "WHERE ref_type='canonical_item' AND ref_id=?",
                (
                    item.metadata.get("source_revision")
                    or f"canonical:{item.item_id}:{item.content_hash}",
                    item.content_hash,
                    payload_content_length(item.payload_kind, item.payload),
                    item.item_id,
                ),
            )
    for assembly_id, raw, detail_ref in connection.execute(
        "SELECT assembly_id, snapshot_json, detail_ref FROM context_assemblies"
    ).fetchall():
        value = localize_plan_value(state, json.loads(raw))
        snapshot = ContextAssemblySnapshot.from_dict(value)
        plan = snapshot.as_sealed_plan()
        snapshot = replace(
            snapshot,
            plan_hash=plan.plan_hash(),
            request_hash=context_request_hash(
                plan,
                snapshot.provider_version,
                projector_id=snapshot.projector_id,
                projector_version=snapshot.projector_version,
                target_format=snapshot.target_format,
                wire_request=snapshot.request_hash_preimage,
            ),
        )
        # registry/seal binding 必须等全部 target snapshot/hash 已写入后安装；
        # remap coordinator 在同一事务末尾执行完整 assembly 验证。
        snapshot.validate_hashes()
        result = connection.execute(
            "UPDATE context_assemblies SET plan_hash=?, request_hash=?, snapshot_json=? WHERE assembly_id=?",
            (
                snapshot.plan_hash,
                snapshot.request_hash,
                canonical_json_text(snapshot.to_dict()),
                assembly_id,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError("fork target assembly snapshot 写入失败")
