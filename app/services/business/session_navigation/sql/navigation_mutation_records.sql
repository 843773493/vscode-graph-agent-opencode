
CREATE TABLE IF NOT EXISTS navigation_mutation_records (
    gateway_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    client_sequence INTEGER NOT NULL,
    queue_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'committed', 'rejected', 'cancelled',
                  'dependency_failed')
    ),
    params_json TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    created_by_operation_id TEXT,
    target_node_id TEXT,
    reserved_node_id TEXT,
    result_node_id TEXT,
    result_node_revision INTEGER,
    committed_catalog_revision INTEGER,
    error_code TEXT,
    error_detail TEXT,
    pending_settlement INTEGER NOT NULL DEFAULT 0,
    holder_id TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    receipt_revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (gateway_id, workspace_id, actor, operation_id)
)
