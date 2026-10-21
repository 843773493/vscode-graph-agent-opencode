
CREATE TABLE IF NOT EXISTS thread_execution_intents (
    admission_idempotency_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    creation_idempotency_key TEXT NOT NULL,
    initial_state TEXT NOT NULL CHECK (initial_state IN ('running', 'idle')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'bound')),
    execution_binding_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL UNIQUE,
    binding_preimage_hash TEXT NOT NULL,
    claim_owner TEXT,
    claim_generation INTEGER,
    last_error TEXT,
    intent_created_at TEXT NOT NULL,
    intent_updated_at TEXT NOT NULL,
    CHECK ((claim_owner IS NULL) = (claim_generation IS NULL)),
    CHECK (claim_generation IS NULL OR claim_generation >= 1)
)
