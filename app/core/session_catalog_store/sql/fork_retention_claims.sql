
CREATE TABLE IF NOT EXISTS fork_retention_claims (
    fork_retention_claim_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    source_lifecycle_generation INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'active', 'released')),
    release_reason TEXT,
    record_created_at TEXT NOT NULL,
    record_updated_at TEXT NOT NULL
)
