ALTER TABLE persistent_task_runs RENAME TO persistent_task_runs_v18;
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
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('manual','interval','cron','external','execution_end')),
    scheduled_for TEXT,
    trigger_payload_json TEXT CHECK (trigger_payload_json IS NULL OR (json_valid(trigger_payload_json) AND json_type(trigger_payload_json) = 'object')),
    UNIQUE (task_id, trigger_key),
    FOREIGN KEY (task_id, definition_revision) REFERENCES persistent_task_definitions(task_id, definition_revision),
    CHECK ((trigger_type IN ('manual','external','execution_end') AND scheduled_for IS NULL) OR (trigger_type IN ('interval','cron') AND scheduled_for IS NOT NULL)),
    CHECK ((trigger_type IN ('external','execution_end')) = (trigger_payload_json IS NOT NULL))
);
INSERT INTO persistent_task_runs SELECT * FROM persistent_task_runs_v18 ORDER BY run_sequence;
DROP TABLE persistent_task_runs_v18;
CREATE INDEX persistent_task_run_pending ON persistent_task_runs(task_id, run_sequence)
WHERE phase IN ('pending','checking','delivering');

CREATE TABLE persistent_task_execution_sources (
    task_id TEXT PRIMARY KEY REFERENCES persistent_tasks(task_id),
    definition_revision INTEGER NOT NULL,
    source_uri TEXT NOT NULL,
    after_commit_id INTEGER NOT NULL CHECK(after_commit_id>=0),
    FOREIGN KEY(task_id,definition_revision) REFERENCES persistent_task_definitions(task_id,definition_revision)
);
