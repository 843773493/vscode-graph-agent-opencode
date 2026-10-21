
    CREATE TABLE IF NOT EXISTS registry_apply_journal (
        apply_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        base_revision INTEGER NOT NULL,
        target_revision INTEGER,
        state TEXT NOT NULL CHECK (
            state IN ('applying', 'committed', 'failed', 'recovery_required')
        ),
        payload_digest TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS registry_apply_journal_state_idx
        ON registry_apply_journal(state, updated_at);
