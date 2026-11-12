CREATE UNIQUE INDEX IF NOT EXISTS idx_navigation_mutation_queue ON navigation_mutation_records(workspace_id, queue_seq)
