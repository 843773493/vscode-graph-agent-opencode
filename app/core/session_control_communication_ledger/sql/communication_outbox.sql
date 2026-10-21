
CREATE TABLE IF NOT EXISTS communication_outbox (
    send_operation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source_gateway_id TEXT NOT NULL,
    source_workspace_id TEXT NOT NULL,
    source_thread_id TEXT NOT NULL,
    communication_id TEXT NOT NULL UNIQUE,
    target_gateway_id TEXT NOT NULL,
    target_workspace_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('question', 'reply', 'progress', 'result')),
    reply_to_communication_id TEXT,
    payload_hash TEXT NOT NULL,
    content TEXT NOT NULL CHECK(length(trim(content)) > 0),
    delivery_policy TEXT NOT NULL,
    request_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'accepted', 'routing', 'target_accepted', 'execution_bound',
        'terminal', 'failed', 'cancelled')),
    latest_receipt TEXT,
    abort_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((kind = 'reply') = (reply_to_communication_id IS NOT NULL))
)
