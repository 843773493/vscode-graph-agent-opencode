
CREATE TABLE IF NOT EXISTS fork_attachment_claims (
    claim_id TEXT PRIMARY KEY,
    copy_operation_id TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    source_attachment_id TEXT NOT NULL,
    target_owner_ref_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    blob_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    operation_preimage_hash TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'owner_reserved', 'committed', 'aborted', 'target_deleted')),
    UNIQUE (copy_operation_id, source_attachment_id, target_owner_ref_id)
)
