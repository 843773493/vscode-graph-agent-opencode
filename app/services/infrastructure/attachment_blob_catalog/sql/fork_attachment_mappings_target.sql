
CREATE INDEX IF NOT EXISTS fork_attachment_mappings_target
ON fork_attachment_mappings(target_session_id, target_thread_id, source_file_id)
