
CREATE TABLE resource_activation_schema_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    activation_schema_version INTEGER NOT NULL CHECK(activation_schema_version >= 1),
    updated_at TEXT NOT NULL
);
CREATE TABLE resource_activation_snapshots (
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    activation_snapshot_id TEXT NOT NULL,
    snapshot_kind TEXT NOT NULL CHECK(snapshot_kind IN ('execution','model_call')),
    parent_execution_snapshot_id TEXT,
    activation_policy_revision TEXT NOT NULL CHECK(length(activation_policy_revision) > 0),
    activation_policy_hash TEXT NOT NULL,
    activation_policy_json TEXT NOT NULL CHECK(json_valid(activation_policy_json)),
    registry_generation INTEGER NOT NULL CHECK(registry_generation >= 0),
    execution_id TEXT NOT NULL CHECK(length(execution_id) > 0),
    turn_id TEXT,
    model_call_id TEXT,
    captured_at TEXT NOT NULL,
    bindings_hash TEXT NOT NULL,
    activation_provenance_hash TEXT NOT NULL,
    binding_count INTEGER NOT NULL CHECK(binding_count >= 0),
    lineage_manifest_digest TEXT NOT NULL,
    lineage_detail_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, thread_id, activation_snapshot_id),
    FOREIGN KEY(session_id, thread_id, execution_id)
        REFERENCES executions(session_id, thread_id, execution_id),
    FOREIGN KEY(session_id, thread_id, parent_execution_snapshot_id)
        REFERENCES resource_activation_snapshots(session_id, thread_id, activation_snapshot_id),
    CHECK((snapshot_kind = 'execution') = (model_call_id IS NULL)),
    CHECK((snapshot_kind = 'execution') = (parent_execution_snapshot_id IS NULL)),
    CHECK(json_valid(lineage_detail_ref)
        AND json_type(lineage_detail_ref, '$.session_id') IS 'text'
        AND json_type(lineage_detail_ref, '$.assembly_id') IS 'text'
        AND json_type(lineage_detail_ref, '$.detail_id') IS 'text')
);
CREATE TRIGGER resource_activation_snapshot_admission
BEFORE INSERT ON resource_activation_snapshots
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM executions e
        WHERE e.session_id = NEW.session_id AND e.thread_id = NEW.thread_id
          AND e.execution_id = NEW.execution_id AND e.turn_id IS NEW.turn_id
    ) THEN RAISE(ABORT, 'resource-activation-snapshot-conflict: execution admission mismatch') END;
    SELECT CASE WHEN NEW.snapshot_kind = 'model_call' AND NOT EXISTS (
        SELECT 1 FROM resource_activation_snapshots p
        WHERE p.session_id = NEW.session_id AND p.thread_id = NEW.thread_id
          AND p.activation_snapshot_id = NEW.parent_execution_snapshot_id
          AND p.snapshot_kind = 'execution' AND p.execution_id = NEW.execution_id
          AND p.turn_id IS NEW.turn_id
          AND p.activation_policy_revision = NEW.activation_policy_revision
          AND p.activation_policy_hash = NEW.activation_policy_hash
          AND p.activation_policy_json = NEW.activation_policy_json
    ) THEN RAISE(ABORT, 'resource-activation-snapshot-conflict: parent execution mismatch') END;
END;
CREATE TABLE resource_activation_bindings (
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    activation_snapshot_id TEXT NOT NULL,
    activation_ordinal INTEGER NOT NULL CHECK(activation_ordinal >= 0),
    resource_id TEXT NOT NULL,
    display_uri TEXT CHECK(display_uri IS NULL OR length(display_uri) > 0),
    resource_kind TEXT NOT NULL,
    owner_scope TEXT NOT NULL,
    facet TEXT NOT NULL,
    revision TEXT NOT NULL,
    availability TEXT NOT NULL,
    content_length INTEGER NOT NULL CHECK(content_length >= 0),
    content_hash TEXT,
    effective_boundary TEXT NOT NULL CHECK(effective_boundary IN ('turn','model_call')),
    captured_registry_generation INTEGER NOT NULL CHECK(captured_registry_generation >= 0),
    source_lineage_digest TEXT NOT NULL,
    snapshot_ref TEXT,
    detail_ref TEXT,
    PRIMARY KEY(session_id, thread_id, activation_snapshot_id, activation_ordinal),
    UNIQUE(session_id, thread_id, activation_snapshot_id, resource_id),
    FOREIGN KEY(session_id, thread_id, activation_snapshot_id)
        REFERENCES resource_activation_snapshots(session_id, thread_id, activation_snapshot_id),
    CHECK(content_hash IS NOT NULL)
);
CREATE TABLE resource_activation_assembly_bindings (
    assembly_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    activation_snapshot_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    selection_manifest_hash TEXT NOT NULL,
    bindings_hash TEXT NOT NULL,
    activation_provenance_hash TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    FOREIGN KEY(session_id, thread_id, activation_snapshot_id)
        REFERENCES resource_activation_snapshots(session_id, thread_id, activation_snapshot_id),
    FOREIGN KEY(assembly_id) REFERENCES context_assemblies(assembly_id)
);
CREATE UNIQUE INDEX resource_activation_execution_owner
    ON resource_activation_snapshots(session_id, thread_id, execution_id)
    WHERE snapshot_kind = 'execution';
CREATE UNIQUE INDEX resource_activation_model_call_owner
    ON resource_activation_snapshots(session_id, thread_id, model_call_id)
    WHERE snapshot_kind = 'model_call';
CREATE INDEX resource_activation_snapshots_turn_index
    ON resource_activation_snapshots(session_id, thread_id, turn_id);
CREATE INDEX resource_activation_bindings_resource_index
    ON resource_activation_bindings(session_id, thread_id, resource_id, revision);
CREATE INDEX resource_activation_assembly_bindings_snapshot_index
    ON resource_activation_assembly_bindings(session_id, thread_id, activation_snapshot_id);
CREATE INDEX resource_activation_assembly_bindings_plan_index
    ON resource_activation_assembly_bindings(session_id, plan_id, plan_hash);
