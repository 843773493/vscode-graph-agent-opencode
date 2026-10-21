CREATE TABLE IF NOT EXISTS assembly_content_nodes (
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    body_json TEXT NOT NULL CHECK(json_valid(body_json)),
    PRIMARY KEY(session_id, thread_id, node_id)
) WITHOUT ROWID;
