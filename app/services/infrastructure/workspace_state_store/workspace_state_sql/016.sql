CREATE TABLE persistent_tasks (
    task_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    creation_key TEXT NOT NULL UNIQUE,
    creation_hash TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    definition_revision INTEGER NOT NULL CHECK (definition_revision >= 1),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active','paused','complete','terminated','needs_attention')),
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE persistent_task_definitions (
    task_id TEXT NOT NULL REFERENCES persistent_tasks(task_id),
    definition_revision INTEGER NOT NULL CHECK (definition_revision >= 1),
    configuration_json TEXT NOT NULL CHECK (json_valid(configuration_json)),
    PRIMARY KEY (task_id, definition_revision)
);

CREATE TABLE persistent_task_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES persistent_tasks(task_id),
    task_revision INTEGER NOT NULL CHECK (task_revision >= 1),
    definition_revision INTEGER NOT NULL,
    trigger_key TEXT NOT NULL,
    trigger_hash TEXT NOT NULL,
    request_id TEXT,
    check_only INTEGER NOT NULL CHECK (check_only IN (0,1)),
    phase TEXT NOT NULL CHECK (phase IN ('pending','checking','delivering','delivered','checked','failed','suppressed')),
    decision_json TEXT CHECK (decision_json IS NULL OR json_valid(decision_json)),
    delivery_json TEXT CHECK (delivery_json IS NULL OR json_valid(delivery_json)),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, trigger_key),
    FOREIGN KEY (task_id, definition_revision) REFERENCES persistent_task_definitions(task_id, definition_revision)
);

CREATE INDEX persistent_task_run_pending ON persistent_task_runs(task_id, created_at, run_id)
WHERE phase IN ('pending','checking','delivering');
