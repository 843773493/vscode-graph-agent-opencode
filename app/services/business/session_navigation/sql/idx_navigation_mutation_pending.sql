CREATE INDEX IF NOT EXISTS idx_navigation_mutation_pending ON navigation_mutation_records(workspace_id, state, queue_seq)
