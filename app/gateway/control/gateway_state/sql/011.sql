
    ALTER TABLE config_source_layers ADD COLUMN previous_payload_json TEXT;
    ALTER TABLE config_source_layers ADD COLUMN backup_path TEXT;
    ALTER TABLE config_pending_candidate ADD COLUMN source_generation INTEGER;
    ALTER TABLE config_active_snapshot ADD COLUMN state TEXT NOT NULL DEFAULT 'active';
    ALTER TABLE config_active_snapshot ADD COLUMN last_error TEXT;
    CREATE UNIQUE INDEX IF NOT EXISTS config_events_idempotency_result_unique
        ON config_events(config_domain, idempotency_key, result)
        WHERE idempotency_key IS NOT NULL;
    CREATE TABLE IF NOT EXISTS config_apply_journal (
        config_domain TEXT NOT NULL,
        apply_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        base_active_revision INTEGER,
        pending_revision INTEGER,
        source_baseline_json TEXT NOT NULL,
        active_baseline_json TEXT NOT NULL,
        registry_revision INTEGER,
        side_effects_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('applying', 'committed', 'failed',
                      'recovery_required', 'compensated')
        ),
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS config_apply_journal_domain_idx
        ON config_apply_journal(config_domain, state, updated_at);
