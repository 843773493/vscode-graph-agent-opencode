
CREATE TABLE IF NOT EXISTS background_consumptions (
    consumption_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES thread_catalog(thread_id),
    agent_id TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    batch_json TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0,1))
)
