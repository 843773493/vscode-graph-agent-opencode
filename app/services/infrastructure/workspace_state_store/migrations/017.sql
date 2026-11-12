CREATE TABLE persistent_task_schedules (
    task_id TEXT PRIMARY KEY REFERENCES persistent_tasks(task_id),
    definition_revision INTEGER NOT NULL,
    anchor_at TEXT NOT NULL,
    next_run_at TEXT NOT NULL,
    FOREIGN KEY (task_id, definition_revision) REFERENCES persistent_task_definitions(task_id, definition_revision)
);

ALTER TABLE persistent_task_runs RENAME TO persistent_task_runs_v16;
CREATE TABLE persistent_task_runs (
    run_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES persistent_tasks(task_id),
    task_revision INTEGER NOT NULL CHECK (task_revision >= 1),
    definition_revision INTEGER NOT NULL,
    trigger_key TEXT NOT NULL,
    trigger_hash TEXT NOT NULL,
    request_id TEXT,
    check_only INTEGER NOT NULL CHECK (check_only IN (0,1)),
    phase TEXT NOT NULL CHECK (phase IN ('pending','checking','delivering','delivered','checked','failed','suppressed','skipped')),
    decision_json TEXT CHECK (decision_json IS NULL OR json_valid(decision_json)),
    delivery_json TEXT CHECK (delivery_json IS NULL OR json_valid(delivery_json)),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('manual','interval','cron')),
    scheduled_for TEXT,
    UNIQUE (task_id, trigger_key),
    FOREIGN KEY (task_id, definition_revision) REFERENCES persistent_task_definitions(task_id, definition_revision),
    CHECK ((trigger_type = 'manual' AND scheduled_for IS NULL) OR (trigger_type <> 'manual' AND scheduled_for IS NOT NULL))
);
INSERT INTO persistent_task_runs (
    run_id, task_id, task_revision, definition_revision, trigger_key, trigger_hash,
    request_id, check_only, phase, decision_json, delivery_json, error, created_at, updated_at,
    trigger_type, scheduled_for
)
SELECT *, 'manual', NULL FROM persistent_task_runs_v16 ORDER BY created_at, run_id;
DROP TABLE persistent_task_runs_v16;
CREATE INDEX persistent_task_run_pending ON persistent_task_runs(task_id, run_sequence)
WHERE phase IN ('pending','checking','delivering');
