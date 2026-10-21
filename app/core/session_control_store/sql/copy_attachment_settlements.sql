
CREATE TABLE IF NOT EXISTS copy_attachment_settlements (
    copy_operation_id TEXT PRIMARY KEY,
    preimage_hash TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    captured_generation INTEGER NOT NULL,
    publication_preimage_hash TEXT NOT NULL,
    claim_ids_json TEXT NOT NULL,
    claim_manifest_sha256 TEXT NOT NULL,
    lease_id TEXT NOT NULL UNIQUE,
    lease_fencing_token INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'published_pending_attachment_commit', 'committed', 'aborted', 'target_deleted'))
)
