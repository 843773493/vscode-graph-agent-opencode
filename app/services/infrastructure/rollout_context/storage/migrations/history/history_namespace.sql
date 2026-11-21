
CREATE TABLE IF NOT EXISTS history_namespace_bindings (
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    source_rollout_id TEXT NOT NULL,
    source_snapshot_sha256 TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    PRIMARY KEY(session_id, thread_id)
);
