
CREATE TABLE IF NOT EXISTS fork_source_captures (
    copy_operation_id TEXT PRIMARY KEY,
    creation_key TEXT UNIQUE,
    source_session_id TEXT NOT NULL,
    source_thread_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL UNIQUE,
    snapshot_relative_locator TEXT NOT NULL UNIQUE,
    fork_id TEXT NOT NULL UNIQUE,
    materialization_id TEXT NOT NULL UNIQUE,
    request_json TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'source_captured', 'materialized', 'aborted')),
    snapshot_sha256 TEXT,
    marker_sha256 TEXT,
    abort_reason TEXT,
    export_release_state TEXT CHECK (export_release_state IN ('pending', 'confirmed')),
    export_release_error TEXT,
    export_release_receipt_json TEXT,
    CHECK (
        (export_release_state IS NULL AND export_release_error IS NULL AND export_release_receipt_json IS NULL)
        OR (export_release_state = 'pending' AND export_release_receipt_json IS NULL)
        OR (export_release_state = 'confirmed' AND export_release_error IS NULL AND export_release_receipt_json IS NOT NULL)
    )
)
