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

PRAGMA user_version = 4;
