
CREATE INDEX IF NOT EXISTS idx_background_tasks_active ON background_tasks(thread_id, status)
WHERE status IN ('pending','running','reconcile_required')
