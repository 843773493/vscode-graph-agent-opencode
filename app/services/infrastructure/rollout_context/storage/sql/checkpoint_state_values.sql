CREATE TABLE IF NOT EXISTS checkpoint_state_values (
    value_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL, thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL, channel_name TEXT NOT NULL,
    channel_version TEXT NOT NULL, serializer_name TEXT NOT NULL,
    value_blob BLOB NOT NULL, value_length INTEGER NOT NULL, value_hash TEXT NOT NULL,
    UNIQUE(session_id, thread_id, checkpoint_ns, channel_name, channel_version)
);
