
CREATE TABLE IF NOT EXISTS session_operation_leases (
    lease_id TEXT PRIMARY KEY,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN (
        'thread_creation', 'board_migration', 'collaboration_fanout',
        'runtime_owner', 'execution', 'context_control',
        'communication_source', 'communication_target', 'federated_call',
        'remote_observation', 'attachment', 'fork_retention',
        'session_catalog_mutation')),
    operation_identity TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    captured_lifecycle_generation INTEGER NOT NULL,
    holder_generation INTEGER NOT NULL CHECK (holder_generation >= 1),
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'active', 'settling', 'completed', 'cancelled', 'failed')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    recovery_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (captured_lifecycle_generation, operation_identity)
)
