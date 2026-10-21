
    CREATE TABLE IF NOT EXISTS gateway_restart_intent (
        intent_id TEXT PRIMARY KEY,
        candidate_ref TEXT NOT NULL UNIQUE,
        candidate_id TEXT NOT NULL,
        base_active_revision INTEGER,
        old_generation TEXT,
        target_generation TEXT NOT NULL,
        fencing_token TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('pending', 'applying', 'active', 'failed',
                      'recovery_required', 'discarded')
        ),
        requested_by TEXT NOT NULL,
        health_proof_json TEXT,
        last_error TEXT,
        requested_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
