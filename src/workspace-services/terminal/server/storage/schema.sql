CREATE TABLE terminal_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    workspace_id TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE terminal_records (
    terminal_id TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload) AND json_extract(payload, '$.terminal_id') = terminal_id)
);
PRAGMA user_version = 1;
