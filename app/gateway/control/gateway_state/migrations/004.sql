
    CREATE TABLE IF NOT EXISTS registry_meta (
        registry_key TEXT PRIMARY KEY,
        revision INTEGER NOT NULL
    );
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
