
CREATE TABLE IF NOT EXISTS navigation_events (
    workspace_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    queue_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    result_state TEXT NOT NULL,
    committed_catalog_revision INTEGER,
    affected_node_ids_json TEXT NOT NULL,
    error_code TEXT,
    error_detail TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, event_seq)
)
