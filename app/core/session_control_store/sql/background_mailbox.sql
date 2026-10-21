
CREATE TABLE IF NOT EXISTS background_mailbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL REFERENCES thread_catalog(thread_id),
    agent_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    message_json TEXT NOT NULL
)
