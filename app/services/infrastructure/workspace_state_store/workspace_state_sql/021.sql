ALTER TABLE persistent_tasks ADD COLUMN application_type TEXT NOT NULL DEFAULT 'automation'
CHECK(application_type IN ('automation','goal'));
ALTER TABLE persistent_tasks ADD COLUMN owner_session_id TEXT;
ALTER TABLE persistent_tasks ADD COLUMN application_json TEXT
CHECK(application_json IS NULL OR (json_valid(application_json) AND json_type(application_json) = 'object'));
ALTER TABLE persistent_tasks ADD COLUMN retired_at TEXT;

CREATE UNIQUE INDEX persistent_task_current_goal ON persistent_tasks(workspace_id,owner_session_id)
WHERE application_type='goal' AND retired_at IS NULL;

CREATE TABLE persistent_task_imports (
    workspace_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES persistent_tasks(task_id),
    PRIMARY KEY(workspace_id,source_key)
);
