CREATE TABLE plugin_install_operations (
    gateway_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed','cancelled','unknown')),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
    error TEXT,
    owner_pid INTEGER,
    owner_created_at REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (gateway_id, operation_id)
);
