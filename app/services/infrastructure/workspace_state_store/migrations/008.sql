
    ALTER TABLE config_events ADD COLUMN relay_state TEXT NOT NULL DEFAULT 'pending';
    ALTER TABLE config_events ADD COLUMN relay_attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE config_events ADD COLUMN relay_last_error TEXT;
    ALTER TABLE config_events ADD COLUMN relay_claimed_by TEXT;
    ALTER TABLE config_events ADD COLUMN relay_claimed_until TEXT;
    ALTER TABLE config_events ADD COLUMN relay_next_attempt_at TEXT
        NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';
    CREATE INDEX IF NOT EXISTS config_events_relay_idx
        ON config_events(relay_state, relay_next_attempt_at, event_seq);
