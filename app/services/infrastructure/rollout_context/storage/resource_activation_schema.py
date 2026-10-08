"""execution-owned activation catalog 的唯一当前 DDL；旧实验 schema 不迁移。"""

from __future__ import annotations

import sqlite3

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.rollout_context.storage.resource_activation_common import (
    SCHEMA_CONFLICT_CODE,
    SCHEMA_UNAVAILABLE_CODE,
    ResourceActivationStoreError,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
)

RESOURCE_ACTIVATION_SCHEMA_VERSION = 4

RESOURCE_ACTIVATION_SCHEMA_SQL = "\nCREATE TABLE resource_activation_schema_state (\n    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),\n    activation_schema_version INTEGER NOT NULL CHECK(activation_schema_version >= 1),\n    updated_at TEXT NOT NULL\n);\nCREATE TABLE resource_activation_snapshots (\n    session_id TEXT NOT NULL,\n    thread_id TEXT NOT NULL,\n    activation_snapshot_id TEXT NOT NULL,\n    snapshot_kind TEXT NOT NULL CHECK(snapshot_kind IN ('execution','model_call')),\n    parent_execution_snapshot_id TEXT,\n    activation_policy_revision TEXT NOT NULL CHECK(length(activation_policy_revision) > 0),\n    activation_policy_hash TEXT NOT NULL,\n    activation_policy_json TEXT NOT NULL CHECK(json_valid(activation_policy_json)),\n    registry_generation INTEGER NOT NULL CHECK(registry_generation >= 0),\n    execution_id TEXT NOT NULL CHECK(length(execution_id) > 0),\n    turn_id TEXT,\n    model_call_id TEXT,\n    captured_at TEXT NOT NULL,\n    bindings_hash TEXT NOT NULL,\n    activation_provenance_hash TEXT NOT NULL,\n    binding_count INTEGER NOT NULL CHECK(binding_count >= 0),\n    lineage_manifest_digest TEXT NOT NULL,\n    lineage_detail_ref TEXT NOT NULL,\n    created_at TEXT NOT NULL,\n    PRIMARY KEY(session_id, thread_id, activation_snapshot_id),\n    FOREIGN KEY(session_id, thread_id, execution_id)\n        REFERENCES executions(session_id, thread_id, execution_id),\n    FOREIGN KEY(session_id, thread_id, parent_execution_snapshot_id)\n        REFERENCES resource_activation_snapshots(session_id, thread_id, activation_snapshot_id),\n    CHECK((snapshot_kind = 'execution') = (model_call_id IS NULL)),\n    CHECK((snapshot_kind = 'execution') = (parent_execution_snapshot_id IS NULL)),\n    CHECK(json_valid(lineage_detail_ref)\n        AND json_type(lineage_detail_ref, '$.session_id') IS 'text'\n        AND json_type(lineage_detail_ref, '$.assembly_id') IS 'text'\n        AND json_type(lineage_detail_ref, '$.detail_id') IS 'text')\n);\nCREATE TRIGGER resource_activation_snapshot_admission\nBEFORE INSERT ON resource_activation_snapshots\nBEGIN\n    SELECT CASE WHEN NOT EXISTS (\n        SELECT 1 FROM executions e\n        WHERE e.session_id = NEW.session_id AND e.thread_id = NEW.thread_id\n          AND e.execution_id = NEW.execution_id AND e.turn_id IS NEW.turn_id\n    ) THEN RAISE(ABORT, 'resource-activation-snapshot-conflict: execution admission mismatch') END;\n    SELECT CASE WHEN NEW.snapshot_kind = 'model_call' AND NOT EXISTS (\n        SELECT 1 FROM resource_activation_snapshots p\n        WHERE p.session_id = NEW.session_id AND p.thread_id = NEW.thread_id\n          AND p.activation_snapshot_id = NEW.parent_execution_snapshot_id\n          AND p.snapshot_kind = 'execution' AND p.execution_id = NEW.execution_id\n          AND p.turn_id IS NEW.turn_id\n          AND p.activation_policy_revision = NEW.activation_policy_revision\n          AND p.activation_policy_hash = NEW.activation_policy_hash\n          AND p.activation_policy_json = NEW.activation_policy_json\n    ) THEN RAISE(ABORT, 'resource-activation-snapshot-conflict: parent execution mismatch') END;\nEND;\nCREATE TABLE resource_activation_bindings (\n    session_id TEXT NOT NULL,\n    thread_id TEXT NOT NULL,\n    activation_snapshot_id TEXT NOT NULL,\n    activation_ordinal INTEGER NOT NULL CHECK(activation_ordinal >= 0),\n    resource_id TEXT NOT NULL,\n    display_uri TEXT CHECK(display_uri IS NULL OR length(display_uri) > 0),\n    resource_kind TEXT NOT NULL,\n    owner_scope TEXT NOT NULL,\n    facet TEXT NOT NULL,\n    revision TEXT NOT NULL,\n    availability TEXT NOT NULL,\n    content_length INTEGER NOT NULL CHECK(content_length >= 0),\n    content_hash TEXT,\n    effective_boundary TEXT NOT NULL CHECK(effective_boundary IN ('turn','model_call')),\n    captured_registry_generation INTEGER NOT NULL CHECK(captured_registry_generation >= 0),\n    source_lineage_digest TEXT NOT NULL,\n    snapshot_ref TEXT,\n    detail_ref TEXT,\n    PRIMARY KEY(session_id, thread_id, activation_snapshot_id, activation_ordinal),\n    UNIQUE(session_id, thread_id, activation_snapshot_id, resource_id),\n    FOREIGN KEY(session_id, thread_id, activation_snapshot_id)\n        REFERENCES resource_activation_snapshots(session_id, thread_id, activation_snapshot_id),\n    CHECK(content_hash IS NOT NULL)\n);\nCREATE TABLE resource_activation_assembly_bindings (\n    assembly_id TEXT PRIMARY KEY,\n    session_id TEXT NOT NULL,\n    thread_id TEXT NOT NULL,\n    activation_snapshot_id TEXT NOT NULL,\n    plan_id TEXT NOT NULL,\n    plan_hash TEXT NOT NULL,\n    request_hash TEXT NOT NULL,\n    selection_manifest_hash TEXT NOT NULL,\n    bindings_hash TEXT NOT NULL,\n    activation_provenance_hash TEXT NOT NULL,\n    bound_at TEXT NOT NULL,\n    FOREIGN KEY(session_id, thread_id, activation_snapshot_id)\n        REFERENCES resource_activation_snapshots(session_id, thread_id, activation_snapshot_id),\n    FOREIGN KEY(assembly_id) REFERENCES context_assemblies(assembly_id)\n);\nCREATE UNIQUE INDEX resource_activation_execution_owner\n    ON resource_activation_snapshots(session_id, thread_id, execution_id)\n    WHERE snapshot_kind = 'execution';\nCREATE UNIQUE INDEX resource_activation_model_call_owner\n    ON resource_activation_snapshots(session_id, thread_id, model_call_id)\n    WHERE snapshot_kind = 'model_call';\nCREATE INDEX resource_activation_snapshots_turn_index\n    ON resource_activation_snapshots(session_id, thread_id, turn_id);\nCREATE INDEX resource_activation_bindings_resource_index\n    ON resource_activation_bindings(session_id, thread_id, resource_id, revision);\nCREATE INDEX resource_activation_assembly_bindings_snapshot_index\n    ON resource_activation_assembly_bindings(session_id, thread_id, activation_snapshot_id);\nCREATE INDEX resource_activation_assembly_bindings_plan_index\n    ON resource_activation_assembly_bindings(session_id, plan_id, plan_hash);\n"

RESOURCE_ACTIVATION_TABLES = (
    "resource_activation_schema_state",
    "resource_activation_snapshots",
    "resource_activation_bindings",
    "resource_activation_assembly_bindings",
)


def read_resource_activation_schema_version(connection: sqlite3.Connection) -> int | None:
    """只读版本 marker；缺 row 是损坏，不等于全新库。"""

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'resource_activation_schema_state'"
    ).fetchone() is None:
        return None
    row = connection.execute(
        "SELECT activation_schema_version FROM resource_activation_schema_state "
        "WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        raise ResourceActivationStoreError(
            SCHEMA_CONFLICT_CODE, "activation schema 缺少版本 marker row"
        )
    return strict_non_negative_int(row[0], field="activation_schema_version")


def require_resource_activation_schema(connection: sqlite3.Connection) -> None:
    """正常读写、rewind/compaction/fork 都要求当前 schema，绝不补建。"""

    version = read_resource_activation_schema_version(connection)
    if version != RESOURCE_ACTIVATION_SCHEMA_VERSION:
        raise ResourceActivationStoreError(
            SCHEMA_UNAVAILABLE_CODE,
            "activation schema 缺失或版本不受支持: "
            f"database={version}, supported={RESOURCE_ACTIVATION_SCHEMA_VERSION}",
        )
    present = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = set(RESOURCE_ACTIVATION_TABLES) - present
    if missing:
        raise ResourceActivationStoreError(
            SCHEMA_UNAVAILABLE_CODE, f"activation schema 缺少当前对象: {sorted(missing)}"
        )


def create_resource_activation_schema(connection: sqlite3.Connection) -> None:
    """仅 fresh rollout 初始化建立当前 schema；已存在的库必须显式拒绝。"""

    present = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name LIKE 'resource_activation_%'"
    ).fetchall()
    if present:
        raise ResourceActivationStoreError(
            SCHEMA_CONFLICT_CODE, "fresh activation 初始化发现既有实验对象"
        )
    connection.executescript(RESOURCE_ACTIVATION_SCHEMA_SQL)
    connection.execute(
        "INSERT INTO resource_activation_schema_state "
        "(singleton_id, activation_schema_version, updated_at) VALUES (1, ?, ?)",
        (RESOURCE_ACTIVATION_SCHEMA_VERSION, utc_now_text()),
    )


__all__ = [
    "RESOURCE_ACTIVATION_SCHEMA_SQL",
    "RESOURCE_ACTIVATION_SCHEMA_VERSION",
    "RESOURCE_ACTIVATION_TABLES",
    "create_resource_activation_schema",
    "require_resource_activation_schema",
]
