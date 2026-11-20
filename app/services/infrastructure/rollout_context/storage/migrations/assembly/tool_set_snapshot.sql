
CREATE TABLE IF NOT EXISTS tool_set_snapshots (
    tool_set_snapshot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    assembly_id TEXT,
    source_revision TEXT NOT NULL,
    tool_set_schema TEXT NOT NULL,
    tool_set_schema_version TEXT NOT NULL,
    tool_policy_version TEXT NOT NULL,
    content_length INTEGER NOT NULL,
    content_hash TEXT,
    protection TEXT NOT NULL,
    availability TEXT NOT NULL,
    tool_policy_json TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, thread_id, plan_id, tool_set_snapshot_id),
    UNIQUE(assembly_id, tool_set_snapshot_id),
    FOREIGN KEY(session_id, thread_id, plan_id)
        REFERENCES context_plans(session_id, thread_id, plan_id),
    CHECK(content_hash IS NOT NULL)
);
