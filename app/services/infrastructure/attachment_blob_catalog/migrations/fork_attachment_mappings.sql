
CREATE TABLE IF NOT EXISTS fork_attachment_mappings (
    copy_operation_id TEXT NOT NULL,
    source_file_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY (copy_operation_id, source_file_id)
)
