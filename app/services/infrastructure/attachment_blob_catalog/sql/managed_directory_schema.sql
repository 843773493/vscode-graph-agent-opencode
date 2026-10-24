CREATE TABLE attachment_directory_metadata (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    workspace_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 4)
);

CREATE TABLE attachment_directories (
    attachment_id TEXT PRIMARY KEY,
    relative_locator TEXT NOT NULL UNIQUE,
    availability TEXT NOT NULL CHECK (availability IN ('available', 'deleting', 'tombstoned')),
    protection TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    unattached_since TEXT,
    gc_after TEXT,
    retention_until TEXT,
    CHECK (length(attachment_id) = 36 AND substr(attachment_id, 1, 4) = 'att_'),
    CHECK ((unattached_since IS NULL AND gc_after IS NULL) OR
           (unattached_since IS NOT NULL AND gc_after IS NOT NULL AND gc_after >= unattached_since))
);

CREATE INDEX attachment_directories_gc ON attachment_directories(gc_after)
    WHERE availability = 'available' AND gc_after IS NOT NULL;

CREATE TABLE attachment_directory_files (
    attachment_id TEXT NOT NULL REFERENCES attachment_directories(attachment_id),
    relative_path TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('original', 'preview')),
    digest TEXT NOT NULL CHECK (length(digest) = 71 AND substr(digest, 1, 7) = 'sha256:'),
    length INTEGER NOT NULL CHECK (length >= 0),
    file_name TEXT,
    mime_type TEXT,
    PRIMARY KEY (attachment_id, relative_path),
    CHECK ((role = 'original' AND relative_path = 'original/content') OR
           (role = 'preview' AND relative_path = 'preview/512.webp'))
);

CREATE TABLE attachment_directory_associations (
    attachment_id TEXT NOT NULL REFERENCES attachment_directories(attachment_id),
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    binding_token TEXT NOT NULL CHECK (length(binding_token) > 0),
    state TEXT NOT NULL CHECK (state IN ('active', 'released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (attachment_id, session_id, thread_id)
);

CREATE INDEX attachment_directory_associations_owner
    ON attachment_directory_associations(session_id, thread_id, state);

CREATE TABLE attachment_legacy_bindings (
    attachment_id TEXT PRIMARY KEY REFERENCES attachment_directories(attachment_id),
    owner_ref_id TEXT NOT NULL UNIQUE,
    source_session_id TEXT NOT NULL,
    source_thread_id TEXT NOT NULL,
    source_blob_id TEXT NOT NULL,
    source_owner_json TEXT NOT NULL CHECK (json_valid(source_owner_json)),
    source_blob_json TEXT NOT NULL CHECK (json_valid(source_blob_json))
);

CREATE TABLE attachment_legacy_fork_payloads (
    record_kind TEXT NOT NULL CHECK (record_kind IN ('claim', 'mapping')),
    record_key TEXT NOT NULL,
    copy_operation_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    PRIMARY KEY (record_kind, record_key)
);

CREATE INDEX attachment_legacy_fork_payloads_operation
    ON attachment_legacy_fork_payloads(copy_operation_id, record_kind);

CREATE TRIGGER attachment_legacy_bindings_no_update BEFORE UPDATE ON attachment_legacy_bindings
BEGIN SELECT RAISE(ABORT, 'legacy attachment provenance is immutable'); END;
CREATE TRIGGER attachment_legacy_bindings_no_delete BEFORE DELETE ON attachment_legacy_bindings
BEGIN SELECT RAISE(ABORT, 'legacy attachment provenance is immutable'); END;
CREATE TRIGGER attachment_legacy_fork_payloads_no_update BEFORE UPDATE ON attachment_legacy_fork_payloads
BEGIN SELECT RAISE(ABORT, 'legacy fork payload is immutable'); END;
CREATE TRIGGER attachment_legacy_fork_payloads_no_delete BEFORE DELETE ON attachment_legacy_fork_payloads
BEGIN SELECT RAISE(ABORT, 'legacy fork payload is immutable'); END;

CREATE TABLE attachment_directory_ingests (
    ingest_idempotency_key TEXT PRIMARY KEY,
    ingest_id TEXT NOT NULL UNIQUE,
    attachment_id TEXT NOT NULL UNIQUE,
    owner_session_id TEXT NOT NULL,
    owner_thread_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    pin_lease_id TEXT NOT NULL,
    pin_fencing_token INTEGER NOT NULL CHECK (pin_fencing_token > 0),
    pin_captured_generation INTEGER NOT NULL CHECK (pin_captured_generation >= 0),
    preimage_hash TEXT NOT NULL,
    staging_relative_locator TEXT NOT NULL UNIQUE,
    relative_locator TEXT NOT NULL UNIQUE,
    digest TEXT NOT NULL,
    length INTEGER NOT NULL CHECK (length >= 0),
    file_name TEXT,
    mime_type TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'durable', 'published', 'aborted')),
    abort_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX attachment_directory_ingests_owner
    ON attachment_directory_ingests(owner_session_id, owner_thread_id, state);

CREATE TABLE attachment_directory_fork_claims (
    claim_id TEXT PRIMARY KEY,
    copy_operation_id TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    operation_preimage_hash TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    source_attachment_id TEXT NOT NULL,
    target_attachment_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    state TEXT NOT NULL CHECK (state IN ('prepared', 'owner_reserved', 'committed', 'aborted', 'target_deleted')),
    export_released INTEGER NOT NULL DEFAULT 0 CHECK (export_released IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX attachment_directory_fork_claims_source
    ON attachment_directory_fork_claims(source_attachment_id, state, export_released);
CREATE INDEX attachment_directory_fork_claims_target
    ON attachment_directory_fork_claims(target_session_id, target_thread_id, state);
CREATE INDEX attachment_directory_fork_claims_operation
    ON attachment_directory_fork_claims(copy_operation_id);

CREATE TABLE attachment_directory_fork_aliases (
    copy_operation_id TEXT NOT NULL,
    source_file_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    PRIMARY KEY (copy_operation_id, source_file_id)
);
CREATE UNIQUE INDEX attachment_directory_fork_aliases_owner
    ON attachment_directory_fork_aliases(target_session_id, target_thread_id, source_file_id);

CREATE TRIGGER attachment_directory_files_no_update BEFORE UPDATE ON attachment_directory_files
BEGIN SELECT RAISE(ABORT, 'attachment original and preview versions are immutable'); END;
CREATE TRIGGER attachment_directory_files_no_delete BEFORE DELETE ON attachment_directory_files
BEGIN SELECT RAISE(ABORT, 'attachment original and preview versions are immutable'); END;
CREATE TRIGGER attachment_directory_fork_aliases_no_update BEFORE UPDATE ON attachment_directory_fork_aliases
BEGIN SELECT RAISE(ABORT, 'attachment fork aliases are immutable'); END;
CREATE TRIGGER attachment_directory_fork_aliases_no_delete BEFORE DELETE ON attachment_directory_fork_aliases
BEGIN SELECT RAISE(ABORT, 'attachment fork aliases are immutable'); END;

PRAGMA user_version = 4;
