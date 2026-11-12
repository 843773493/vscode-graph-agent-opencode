
CREATE TABLE IF NOT EXISTS subtree_delete_records (
    subtree_delete_idempotency_key TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    root_node_id TEXT NOT NULL,
    frozen_node_ids TEXT NOT NULL,
    frozen_session_locators TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'deleting', 'draining', 'completed', 'aborted')),
    abort_reason TEXT,
    record_created_at TEXT NOT NULL,
    record_updated_at TEXT NOT NULL,
    drained_session_ids TEXT NOT NULL DEFAULT '[]'
)
