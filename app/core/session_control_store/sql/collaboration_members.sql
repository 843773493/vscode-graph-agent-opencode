
CREATE TABLE IF NOT EXISTS collaboration_members (
    delegation_id TEXT PRIMARY KEY,
    creation_idempotency_key TEXT NOT NULL UNIQUE,
    membership_preimage_hash TEXT NOT NULL,
    registered_revision INTEGER NOT NULL CHECK (registered_revision > 0),
    coordinator_session_id TEXT NOT NULL,
    coordinator_thread_id TEXT NOT NULL,
    child_thread_id TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    subagent_type TEXT NOT NULL,
    title TEXT NOT NULL,
    task_seed TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('registering', 'published', 'cancelled')),
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
