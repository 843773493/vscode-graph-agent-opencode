
    CREATE TABLE IF NOT EXISTS gateway_config (
        config_key TEXT PRIMARY KEY,
        config_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS gateway_workspace_registry (
        workspace_id TEXT PRIMARY KEY,
        position INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_account (
        user_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted_at TEXT
    );
    CREATE TABLE IF NOT EXISTS user_access_lease (
        user_id TEXT PRIMARY KEY REFERENCES user_account(user_id) ON DELETE CASCADE,
        lease_generation INTEGER NOT NULL,
        access_session_id TEXT NOT NULL UNIQUE,
        client_label TEXT,
        acquired_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_view_state (
        user_id TEXT NOT NULL REFERENCES user_account(user_id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        turn_anchor TEXT,
        scroll_offset REAL NOT NULL DEFAULT 0,
        follow_latest INTEGER NOT NULL DEFAULT 0,
        projection_version INTEGER NOT NULL DEFAULT 1,
        tool_details_expanded INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, workspace_id, session_id)
    );
    CREATE TABLE IF NOT EXISTS guest_tracking (
        guest_id TEXT PRIMARY KEY,
        tracking_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
