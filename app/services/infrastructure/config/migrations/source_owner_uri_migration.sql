
            DROP INDEX IF EXISTS config_source_journal_path_idx;
            CREATE TABLE config_source_journal__new (
                source_key TEXT NOT NULL,
                source_generation INTEGER NOT NULL,
                source_event_id TEXT NOT NULL UNIQUE,
                uri TEXT,
                presence TEXT NOT NULL CHECK (presence IN ('present', 'absent')),
                layer_revision INTEGER NOT NULL,
                layer_digest TEXT,
                previous_digest TEXT,
                origin TEXT NOT NULL,
                fanout_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (source_key, source_generation)
            );
            INSERT INTO config_source_journal__new(
                source_key, source_generation, source_event_id, uri, presence,
                layer_revision, layer_digest, previous_digest, origin, fanout_id,
                created_at
            )
            SELECT source_key, source_generation, source_event_id, NULL, presence,
                   layer_revision, layer_digest, previous_digest, origin, fanout_id,
                   created_at
            FROM config_source_journal;
            DROP TABLE config_source_journal;
            ALTER TABLE config_source_journal__new RENAME TO config_source_journal;
