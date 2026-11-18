
CREATE TABLE IF NOT EXISTS background_consumptions (
    consumption_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL REFERENCES thread_catalog(thread_id),
    agent_id TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    source_id TEXT,
    after_sequence INTEGER NOT NULL CHECK (after_sequence >= 0),
    through_sequence INTEGER NOT NULL CHECK (through_sequence >= after_sequence),
    message_count INTEGER NOT NULL CHECK (message_count >= 0),
    interrupted INTEGER NOT NULL CHECK (interrupted IN (0,1)),
    timed_out INTEGER NOT NULL CHECK (timed_out IN (0,1)),
    collected_at TEXT NOT NULL,
    tool_result_identity_json TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0,1))
)
