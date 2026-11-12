
    CREATE TABLE IF NOT EXISTS config_revision_meta (
        config_domain TEXT PRIMARY KEY,
        next_revision INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config_active_snapshot (
        config_domain TEXT PRIMARY KEY,
        active_revision INTEGER NOT NULL,
        candidate_id TEXT,
        payload_json TEXT NOT NULL,
        source_baseline_json TEXT NOT NULL,
        source_generation INTEGER NOT NULL,
        layer_revisions_json TEXT NOT NULL,
        layer_digests_json TEXT NOT NULL,
        effective_digest TEXT NOT NULL,
        secret_bindings_json TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        promoted_generation TEXT,
        promoted_apply_id TEXT,
        promoted_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config_pending_candidate (
        config_domain TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        pending_revision INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        source_baseline_json TEXT NOT NULL,
        candidate_digest TEXT NOT NULL,
        effective_digest TEXT NOT NULL,
        target_generation TEXT,
        fencing_token TEXT,
        state TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL,
        last_attempt_id TEXT,
        last_apply_id TEXT,
        PRIMARY KEY (config_domain, candidate_id),
        UNIQUE (config_domain, idempotency_key)
    );
    CREATE TABLE IF NOT EXISTS config_apply_claim (
        config_domain TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        apply_id TEXT NOT NULL UNIQUE,
        owner TEXT NOT NULL,
        base_active_revision INTEGER,
        target_generation TEXT,
        lease_expires_at TEXT NOT NULL,
        fencing_token TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config_events (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        config_domain TEXT NOT NULL,
        candidate_id TEXT,
        attempt_id TEXT,
        apply_id TEXT,
        idempotency_key TEXT,
        commit_revision INTEGER,
        active_revision INTEGER,
        pending_revision INTEGER,
        source TEXT NOT NULL,
        result TEXT NOT NULL,
        changed_paths_json TEXT NOT NULL,
        applied_paths_json TEXT NOT NULL,
        deferred_paths_json TEXT NOT NULL,
        error TEXT,
        occurred_at TEXT NOT NULL
    );
