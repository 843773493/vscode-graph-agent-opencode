CREATE TABLE IF NOT EXISTS checkpoint_channels (
    checkpoint_id TEXT NOT NULL, channel_name TEXT NOT NULL,
    storage_kind TEXT NOT NULL, value_state TEXT NOT NULL, channel_version TEXT,
    value_id INTEGER, value_session_id TEXT, value_thread_id TEXT,
    context_view_id TEXT, updated_index INTEGER, created_at TEXT NOT NULL,
    PRIMARY KEY(checkpoint_id, channel_name)
);
CREATE INDEX IF NOT EXISTS checkpoint_channels_value ON checkpoint_channels(value_id);
