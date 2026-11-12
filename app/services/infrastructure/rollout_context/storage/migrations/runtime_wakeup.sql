
CREATE TABLE IF NOT EXISTS runtime_execution_wakeups (
    admission_idempotency_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_item_id TEXT NOT NULL UNIQUE,
    execution_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    message_created_at TEXT NOT NULL,
    gateway_id TEXT,
    request_id TEXT,
    lease_id TEXT NOT NULL,
    lifecycle_generation INTEGER NOT NULL,
    lease_fencing_token INTEGER NOT NULL,
    delivery_policy TEXT NOT NULL,
    dispatch_generation INTEGER NOT NULL DEFAULT 0,
    dispatch_receipt_json TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending','dispatched')),
    last_error TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runtime_execution_wakeup_state
    ON runtime_execution_wakeups(state, message_created_at, admission_idempotency_key);
