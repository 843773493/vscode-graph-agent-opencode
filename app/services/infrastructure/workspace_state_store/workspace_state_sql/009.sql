
    CREATE TABLE IF NOT EXISTS config_event_relay_delivery (
        event_id TEXT NOT NULL REFERENCES config_events(event_id) ON DELETE CASCADE,
        consumer_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('claimed', 'delivered', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        claimed_until TEXT,
        next_attempt_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (event_id, consumer_id)
    );
    CREATE INDEX IF NOT EXISTS config_event_relay_delivery_due_idx
        ON config_event_relay_delivery(consumer_id, state, next_attempt_at, event_id);
