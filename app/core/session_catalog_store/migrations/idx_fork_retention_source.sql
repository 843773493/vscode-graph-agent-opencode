CREATE INDEX IF NOT EXISTS idx_fork_retention_source ON fork_retention_claims(workspace_id, source_session_id, state)
