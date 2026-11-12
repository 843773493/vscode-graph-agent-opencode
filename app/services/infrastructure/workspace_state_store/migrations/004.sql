
    CREATE TABLE IF NOT EXISTS config_source_owner (
        source_key TEXT PRIMARY KEY,
        next_generation INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config_source_journal (
        source_key TEXT NOT NULL,
        source_generation INTEGER NOT NULL,
        source_event_id TEXT NOT NULL UNIQUE,
        source_path TEXT NOT NULL,
        presence TEXT NOT NULL CHECK (presence IN ('present', 'absent')),
        layer_revision INTEGER NOT NULL,
        layer_digest TEXT,
        previous_digest TEXT,
        origin TEXT NOT NULL,
        fanout_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (source_key, source_generation)
    );
    CREATE TABLE IF NOT EXISTS config_source_fanout (
        source_key TEXT NOT NULL,
        source_generation INTEGER NOT NULL,
        workspace_id TEXT NOT NULL,
        status TEXT NOT NULL,
        layer_revision INTEGER,
        layer_digest TEXT,
        result TEXT,
        error TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_key, source_generation, workspace_id),
        FOREIGN KEY (source_key, source_generation)
            REFERENCES config_source_journal(source_key, source_generation)
            ON DELETE CASCADE
    );
