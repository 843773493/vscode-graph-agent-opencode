
CREATE TABLE IF NOT EXISTS fork_retention_releases (
    fork_retention_claim_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    source_thread_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    source_lifecycle_generation INTEGER NOT NULL CHECK (source_lifecycle_generation >= 1),
    copy_operation_id TEXT NOT NULL,
    capture_preimage_hash TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    release_reason TEXT NOT NULL CHECK (length(release_reason) > 0),
    preimage_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'committed'))
)
