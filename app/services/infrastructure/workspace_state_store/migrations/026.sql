CREATE TABLE session_goal_operations (
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, session_id, operation_id)
);
