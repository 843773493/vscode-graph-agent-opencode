CREATE INDEX IF NOT EXISTS idx_attachment_owner_refs_session ON attachment_owner_refs(owner_session_id, state)
