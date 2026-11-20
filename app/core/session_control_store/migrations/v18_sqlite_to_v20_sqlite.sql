ALTER TABLE background_tasks ADD COLUMN result_json TEXT;
ALTER TABLE collaboration_members ADD COLUMN team_id TEXT;
ALTER TABLE collaboration_members ADD COLUMN work_mode TEXT CHECK(work_mode IN ('write','read_only'));
DROP TABLE background_consumptions;
