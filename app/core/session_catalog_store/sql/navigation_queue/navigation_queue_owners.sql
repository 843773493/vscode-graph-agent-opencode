
CREATE TABLE IF NOT EXISTS navigation_queue_owners (
    workspace_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    owner_generation INTEGER NOT NULL,
    updated_at TEXT NOT NULL
)
