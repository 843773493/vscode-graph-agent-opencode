ALTER TABLE persistent_task_runs ADD COLUMN evaluation_json TEXT
CHECK(evaluation_json IS NULL OR (json_valid(evaluation_json) AND json_type(evaluation_json) = 'object'));
