"""full_rollout_copy 的 activation catalog target-local 重造。

activation snapshot/binding/assembly binding 是 thread-owned 运行事实：复制到
target 后必须生成 target-local ``activation_snapshot_id`` 与 assembly binding，
并把 source→target 映射交给统一的 ``fork_identity_mappings`` lineage。source
owner 的 operational ref 不得直接用于 target lookup。

这三张表带显式 FOREIGN KEY，先整体移除再按 target identity 重写的同一事务提交；
不读取 source 文件、URI 或当前 Registry。
"""

from __future__ import annotations

import json
import sqlite3

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_common import (
    ASSEMBLY_BINDING_COLUMNS,
    BINDING_COLUMNS,
    SNAPSHOT_COLUMNS,
)
from app.services.mapping.itemized.projection import build_projection_evidence

_TABLES = (
    "resource_activation_assembly_bindings",
    "resource_activation_bindings",
    "resource_activation_snapshots",
)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def rewrite_full_copy_activation_catalog(state: FullCopyRemapState) -> None:
    """在 target 事务内重造 activation catalog；无 activation schema 时短路。"""

    connection = state.connection
    if not _table_exists(connection, "resource_activation_snapshots"):
        return
    target_session_id = state.target_session_id
    maps = state.maps

    def identity(entity_type: str, value: object) -> str:
        # activation 引用必须精确解析到 target-local 身份；通用 mapped() 对未登记
        # 值会回填 source id，这里必须 fail closed 而不是静默保留 source identity。
        text_value = required_text(value, field=f"activation.{entity_type}.id")
        target_value = maps.get(entity_type, {}).get(text_value)
        if target_value is None:
            raise RuntimeError(
                "full_rollout_copy activation 引用缺 target-local mapping: "
                f"{entity_type}={text_value}"
            )
        return target_value

    def optional_identity(entity_type: str, value: object) -> str | None:
        if value is None:
            return None
        return identity(entity_type, value)

    snapshot_rows = connection.execute(
        f"SELECT {','.join(SNAPSHOT_COLUMNS)} FROM resource_activation_snapshots "
        "ORDER BY session_id, thread_id, activation_snapshot_id"
    ).fetchall()
    binding_rows = connection.execute(
        f"SELECT {','.join(BINDING_COLUMNS)} FROM resource_activation_bindings "
        "ORDER BY session_id, thread_id, activation_snapshot_id, activation_ordinal"
    ).fetchall()
    assembly_rows = connection.execute(
        f"SELECT {','.join(ASSEMBLY_BINDING_COLUMNS)} "
        "FROM resource_activation_assembly_bindings ORDER BY assembly_id"
    ).fetchall()

    # 三张表带显式 FK，先整体移除私有副本，再按 target identity 完整重写。
    connection.execute("PRAGMA defer_foreign_keys = ON")
    for table in _TABLES:
        connection.execute(f"DELETE FROM {table}")

    for raw in snapshot_rows:
        record = dict(zip(SNAPSHOT_COLUMNS, raw, strict=True))
        values = (
            target_session_id,
            record["thread_id"],
            identity("activation_snapshot", record["activation_snapshot_id"]),
            record["snapshot_kind"],
            optional_identity(
                "activation_snapshot", record["parent_turn_snapshot_id"]
            ),
            record["activation_policy_revision"],
            record["activation_policy_hash"],
            record["registry_generation"],
            identity("turn", record["turn_id"]),
            optional_identity("model_call", record["model_call_id"]),
            record["captured_at"],
            record["bindings_hash"],
            record["activation_provenance_hash"],
            record["binding_count"],
            record["lineage_manifest_digest"],
            identity("detail", record["lineage_detail_ref"]),
            record["created_at"],
        )
        connection.execute(
            "INSERT INTO resource_activation_snapshots "
            f"({','.join(SNAPSHOT_COLUMNS)}) VALUES "
            f"({','.join('?' for _ in SNAPSHOT_COLUMNS)})",
            values,
        )

    for raw in binding_rows:
        record = dict(zip(BINDING_COLUMNS, raw, strict=True))
        values = (
            target_session_id,
            record["thread_id"],
            identity("activation_snapshot", record["activation_snapshot_id"]),
            non_negative_int(
                record["activation_ordinal"], field="activation_ordinal"
            ),
            record["resource_id"],
            record["display_uri"],
            record["resource_kind"],
            record["owner_scope"],
            record["facet"],
            record["revision"],
            record["availability"],
            record["content_length"],
            record["content_hash"],
            record["redacted_stable_digest"],
            record["effective_boundary"],
            record["captured_registry_generation"],
            record["source_lineage_digest"],
            optional_identity("detail", record["snapshot_ref"]),
            optional_identity("detail", record["detail_ref"]),
        )
        connection.execute(
            "INSERT INTO resource_activation_bindings "
            f"({','.join(BINDING_COLUMNS)}) VALUES "
            f"({','.join('?' for _ in BINDING_COLUMNS)})",
            values,
        )

    for raw in assembly_rows:
        record = dict(zip(ASSEMBLY_BINDING_COLUMNS, raw, strict=True))
        target_assembly_id = identity("assembly", record["assembly_id"])
        row = connection.execute(
            "SELECT plan_hash, request_hash, snapshot_json FROM context_assemblies "
            "WHERE assembly_id = ?",
            (target_assembly_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "full_rollout_copy activation binding 缺少 target assembly: "
                f"{target_assembly_id}"
            )
        snapshot = ContextAssemblySnapshot.from_dict(json.loads(row[2]))
        evidence = build_projection_evidence(
            snapshot.as_sealed_plan(), projection="assembly-seal"
        )
        values = (
            target_assembly_id,
            target_session_id,
            record["thread_id"],
            identity("activation_snapshot", record["activation_snapshot_id"]),
            identity("plan", record["plan_id"]),
            required_text(row[0], field="context_assemblies.plan_hash"),
            required_text(row[1], field="context_assemblies.request_hash"),
            evidence.selection_manifest_hash,
            record["bindings_hash"],
            record["activation_provenance_hash"],
            record["bound_at"],
        )
        connection.execute(
            "INSERT INTO resource_activation_assembly_bindings "
            f"({','.join(ASSEMBLY_BINDING_COLUMNS)}) VALUES "
            f"({','.join('?' for _ in ASSEMBLY_BINDING_COLUMNS)})",
            values,
        )

    if _table_exists(connection, "resource_activation_migration_losses"):
        # migration loss 描述 source 升级时的 quarantine，assembly_id 指向 source
        # 而非 target；复制后是悬空事实，必须移除而不是改写成 target session。
        connection.execute("DELETE FROM resource_activation_migration_losses")


__all__ = ["rewrite_full_copy_activation_catalog"]
