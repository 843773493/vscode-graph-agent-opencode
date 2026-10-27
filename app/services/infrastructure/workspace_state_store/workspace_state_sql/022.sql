ALTER TABLE persistent_task_runs ADD COLUMN generation_json TEXT
CHECK(generation_json IS NULL OR (json_valid(generation_json) AND json_type(generation_json)='object'));

CREATE TABLE persistent_task_generator_imports (
    task_id TEXT PRIMARY KEY REFERENCES persistent_tasks(task_id),
    source_hash TEXT NOT NULL,
    files_json TEXT NOT NULL CHECK(json_valid(files_json) AND json_type(files_json)='object')
);
