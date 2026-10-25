
    CREATE TABLE IF NOT EXISTS workspace_activity (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL,
        status TEXT NOT NULL,
        summary TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS workspace_event_cursors (
        cursor_key TEXT PRIMARY KEY,
        cursor_value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
