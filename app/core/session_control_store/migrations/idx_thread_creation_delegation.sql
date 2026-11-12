CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_creation_delegation ON thread_creation_records(delegation_id) WHERE delegation_id IS NOT NULL
