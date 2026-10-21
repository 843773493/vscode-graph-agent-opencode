
CREATE TABLE IF NOT EXISTS thread_catalog (
    thread_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('main', 'child')),
    created_at TEXT NOT NULL,
    graph_id TEXT,
    graph_revision INTEGER CHECK (graph_revision >= 1),
    graph_schema_hash TEXT,
    capability_profile_hash TEXT
)
