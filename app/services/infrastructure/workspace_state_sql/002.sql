
    CREATE TABLE IF NOT EXISTS config_source_layers (
        config_key TEXT PRIMARY KEY,
        source_path TEXT NOT NULL,
        presence TEXT NOT NULL CHECK (presence IN ('present', 'absent')),
        config_version INTEGER NOT NULL,
        payload_json TEXT,
        layer_revision INTEGER NOT NULL,
        layer_digest TEXT,
        source_generation INTEGER NOT NULL,
        previous_digest TEXT,
        updated_at TEXT NOT NULL
    );
