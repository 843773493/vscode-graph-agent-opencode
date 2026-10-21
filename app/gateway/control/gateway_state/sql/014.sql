
    ALTER TABLE gateway_restart_intent ADD COLUMN gateway_id TEXT;
    ALTER TABLE gateway_restart_intent ADD COLUMN expires_at TEXT;
    CREATE INDEX IF NOT EXISTS gateway_restart_intent_gateway_idx
        ON gateway_restart_intent(gateway_id, state, updated_at);
