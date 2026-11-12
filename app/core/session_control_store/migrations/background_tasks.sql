
CREATE TABLE IF NOT EXISTS background_tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL REFERENCES thread_catalog(thread_id),
    agent_id TEXT NOT NULL,
    descriptor_json TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending','running','reconcile_required','completed','cancelled','failed','deleted')),
    lease_id TEXT NOT NULL REFERENCES session_operation_leases(lease_id),
    fencing_token INTEGER NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    runtime_generation INTEGER NOT NULL DEFAULT 0,
    external_effect_pending INTEGER NOT NULL DEFAULT 0 CHECK (external_effect_pending IN (0,1)),
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    metadata_json TEXT NOT NULL
)
