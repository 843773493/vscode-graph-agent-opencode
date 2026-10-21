
CREATE TABLE IF NOT EXISTS attachment_ingest_records (
    ingest_idempotency_key TEXT PRIMARY KEY,
    ingest_id TEXT NOT NULL UNIQUE,
    owner_session_id TEXT NOT NULL,
    owner_thread_id TEXT NOT NULL,
    pin_lease_id TEXT NOT NULL,
    pin_fencing_token INTEGER NOT NULL CHECK (pin_fencing_token >= 1),
    pin_captured_generation INTEGER NOT NULL CHECK (pin_captured_generation >= 0),
    preimage_hash TEXT NOT NULL,
    staging_relative_locator TEXT NOT NULL UNIQUE,
    max_bytes INTEGER NOT NULL CHECK (max_bytes > 0),
    state TEXT NOT NULL CHECK (state IN ('preparing', 'hashed', 'published', 'aborted')),
    digest TEXT,
    blob_id TEXT,
    length INTEGER,
    abort_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
