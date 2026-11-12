
CREATE INDEX IF NOT EXISTS idx_fork_attachment_claims_blob
ON fork_attachment_claims(blob_id, state)
