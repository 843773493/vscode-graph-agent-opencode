
CREATE TABLE IF NOT EXISTS context_plans (
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    plan_creation_idempotency_key TEXT NOT NULL CHECK(length(plan_creation_idempotency_key) > 0),
    creation_hash TEXT NOT NULL CHECK(length(creation_hash) > 0),
    creation_json TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    draft_hash TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    plan_state TEXT NOT NULL CHECK(plan_state IN ('unsealed', 'sealed')),
    assembly_id TEXT,
    seal_idempotency_key TEXT,
    seal_hash TEXT,
    seal_input_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(session_id, thread_id, plan_id),
    UNIQUE(session_id, thread_id, plan_creation_idempotency_key),
    UNIQUE(session_id, thread_id, assembly_id),
    CHECK(length(plan_id) > 0),
    CHECK(
        (plan_state='unsealed' AND seal_input_hash IS NULL)
        OR (plan_state='sealed' AND seal_input_hash IS NOT NULL AND length(seal_input_hash) > 0)
    ),
    CHECK(
        (plan_state = 'unsealed' AND assembly_id IS NULL
            AND seal_idempotency_key IS NULL AND seal_hash IS NULL)
        OR (plan_state = 'sealed' AND assembly_id IS NOT NULL AND length(assembly_id) > 0
            AND seal_idempotency_key IS NOT NULL AND length(seal_idempotency_key) > 0
            AND seal_hash IS NOT NULL AND length(seal_hash) > 0)
    )
);
CREATE TABLE IF NOT EXISTS context_plan_refs (
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    ref_thread_id TEXT NOT NULL,
    ref_type TEXT NOT NULL CHECK(ref_type IN ('canonical_item', 'request_only')),
    ref_id TEXT NOT NULL,
    ref_json TEXT NOT NULL,
    PRIMARY KEY(session_id, thread_id, plan_id, ref_type, ref_id, ref_thread_id),
    FOREIGN KEY(session_id, thread_id, plan_id)
        REFERENCES context_plans(session_id, thread_id, plan_id)
);
CREATE TABLE IF NOT EXISTS context_plan_contributions (
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    contribution_id TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    PRIMARY KEY(session_id, thread_id, plan_id, contribution_id),
    FOREIGN KEY(session_id, thread_id, plan_id)
        REFERENCES context_plans(session_id, thread_id, plan_id)
);
CREATE TABLE IF NOT EXISTS context_plan_seal_failures (
    failure_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    seal_idempotency_key TEXT NOT NULL,
    error_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id, thread_id, plan_id)
        REFERENCES context_plans(session_id, thread_id, plan_id)
);
