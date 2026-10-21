
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('folder', 'session')),
    parent_node_id TEXT REFERENCES nodes(node_id),
    display_name TEXT NOT NULL,
    title_source TEXT CHECK (title_source IN ('default', 'user', 'auto')),
    state TEXT NOT NULL CHECK (state IN ('active', 'deleting')),
    revision INTEGER NOT NULL DEFAULT 1,
    workspace_id TEXT NOT NULL,
    created_at TEXT,
    storage_relative_locator TEXT,
    main_thread_id TEXT,
    session_kind TEXT CHECK (session_kind IN ('normal', 'context_fork')),
    current_agent_id TEXT CHECK (length(current_agent_id) > 0),
    current_provider_id TEXT CHECK (current_provider_id IS NULL OR length(current_provider_id) > 0),
    context_source_session_id TEXT,
    generation_origin_json TEXT CHECK (generation_origin_json IS NULL OR json_valid(generation_origin_json)),
    updated_at TEXT,
    CHECK (
        (kind = 'session'
            AND created_at IS NOT NULL
            AND storage_relative_locator IS NOT NULL
            AND main_thread_id IS NOT NULL
            AND title_source IS NOT NULL
            AND session_kind IS NOT NULL
            AND current_agent_id IS NOT NULL
            AND updated_at IS NOT NULL
            AND ((session_kind = 'normal' AND context_source_session_id IS NULL)
                 OR (session_kind = 'context_fork' AND context_source_session_id IS NOT NULL
                     AND length(context_source_session_id) > 0)))
        OR (kind = 'folder'
            AND created_at IS NULL
            AND storage_relative_locator IS NULL
            AND main_thread_id IS NULL
            AND title_source IS NULL
            AND session_kind IS NULL
            AND current_agent_id IS NULL
            AND current_provider_id IS NULL
            AND context_source_session_id IS NULL
            AND generation_origin_json IS NULL
            AND updated_at IS NULL)
    ),
    UNIQUE (workspace_id, main_thread_id),
    UNIQUE (workspace_id, storage_relative_locator)
)
