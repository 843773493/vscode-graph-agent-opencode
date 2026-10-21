
    CREATE TABLE IF NOT EXISTS gateway_runtime_generation (
        config_domain TEXT NOT NULL,
        generation_id TEXT PRIMARY KEY,
        process_id INTEGER,
        loaded_source TEXT NOT NULL CHECK (loaded_source IN ('active', 'pending')),
        candidate_id TEXT,
        active_revision INTEGER,
        pending_revision INTEGER,
        candidate_digest TEXT,
        effective_digest TEXT NOT NULL,
        secret_binding_digest TEXT,
        fencing_token TEXT,
        listener_state TEXT NOT NULL CHECK (
            listener_state IN ('reserved', 'serving', 'draining', 'closed')
        ),
        state TEXT NOT NULL CHECK (
            state IN ('starting', 'healthy', 'active', 'failed', 'closed')
        ),
        health_proof_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS gateway_runtime_generation_domain_idx
        ON gateway_runtime_generation(config_domain, state, updated_at);
