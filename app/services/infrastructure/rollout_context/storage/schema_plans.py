"""schema4 plan registry DDL；由 schema owner 的初始化/显式升级调用。"""

from __future__ import annotations

PLAN_REGISTRY_SCHEMA_SQL = """
CREATE TABLE context_plans (
    session_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    registration_origin TEXT NOT NULL DEFAULT 'runtime'
        CHECK(registration_origin IN ('runtime', 'schema3_import', 'fork_import')),
    source_provenance_json TEXT,
    source_manifest_json TEXT,
    plan_creation_idempotency_key TEXT,
    creation_hash TEXT,
    creation_json TEXT,
    draft_json TEXT,
    draft_hash TEXT,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    plan_state TEXT NOT NULL CHECK(plan_state IN ('unsealed', 'sealed')),
    assembly_id TEXT,
    seal_idempotency_key TEXT,
    seal_hash TEXT,
    seal_input_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(session_id, plan_id),
    UNIQUE(session_id, plan_creation_idempotency_key),
    UNIQUE(session_id, assembly_id),
    CHECK(length(plan_id) > 0),
    CHECK(
        (registration_origin = 'runtime' AND source_provenance_json IS NULL
            AND source_manifest_json IS NULL
            AND plan_creation_idempotency_key IS NOT NULL AND length(plan_creation_idempotency_key) > 0
            AND creation_hash IS NOT NULL AND length(creation_hash) > 0
            AND creation_json IS NOT NULL AND draft_json IS NOT NULL AND draft_hash IS NOT NULL
            AND ((plan_state='unsealed' AND seal_input_hash IS NULL)
                OR (plan_state='sealed' AND seal_input_hash IS NOT NULL AND length(seal_input_hash) > 0)))
        OR (registration_origin IN ('schema3_import', 'fork_import')
            AND source_provenance_json IS NOT NULL AND length(source_provenance_json) > 0
            AND source_manifest_json IS NOT NULL AND length(source_manifest_json) > 0
            AND plan_state = 'sealed' AND revision = 0
            AND plan_creation_idempotency_key IS NULL AND creation_hash IS NULL
            AND creation_json IS NULL AND draft_json IS NULL AND draft_hash IS NULL AND seal_input_hash IS NULL)
    ),
    CHECK(
        (plan_state = 'unsealed' AND assembly_id IS NULL
            AND seal_idempotency_key IS NULL AND seal_hash IS NULL)
        OR (plan_state = 'sealed' AND assembly_id IS NOT NULL AND length(assembly_id) > 0
            AND seal_idempotency_key IS NOT NULL AND length(seal_idempotency_key) > 0
            AND seal_hash IS NOT NULL AND length(seal_hash) > 0)
    )
);
CREATE TABLE context_plan_refs (
    session_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    ref_type TEXT NOT NULL CHECK(ref_type IN ('canonical_item', 'request_only')),
    ref_id TEXT NOT NULL,
    ref_json TEXT NOT NULL,
    PRIMARY KEY(session_id, plan_id, ref_type, ref_id),
    FOREIGN KEY(session_id, plan_id) REFERENCES context_plans(session_id, plan_id)
);
CREATE TABLE context_plan_contributions (
    session_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    contribution_id TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    PRIMARY KEY(session_id, plan_id, contribution_id),
    FOREIGN KEY(session_id, plan_id) REFERENCES context_plans(session_id, plan_id)
);
CREATE TABLE context_plan_seal_failures (
    failure_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    seal_idempotency_key TEXT NOT NULL,
    error_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id, plan_id) REFERENCES context_plans(session_id, plan_id)
);
"""


TOOL_SET_SNAPSHOT_SCHEMA_SQL = """
CREATE TABLE tool_set_snapshots (
    tool_set_snapshot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    assembly_id TEXT,
    source_revision TEXT NOT NULL,
    tool_set_schema TEXT NOT NULL,
    tool_set_schema_version TEXT NOT NULL,
    tool_policy_version TEXT NOT NULL,
    content_length INTEGER NOT NULL,
    content_hash TEXT,
    redacted_stable_digest TEXT,
    protection TEXT NOT NULL,
    availability TEXT NOT NULL,
    tool_policy_json TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, plan_id, tool_set_snapshot_id),
    UNIQUE(assembly_id, tool_set_snapshot_id),
    FOREIGN KEY(session_id, plan_id) REFERENCES context_plans(session_id, plan_id),
    CHECK((content_hash IS NOT NULL) != (redacted_stable_digest IS NOT NULL))
);
"""
