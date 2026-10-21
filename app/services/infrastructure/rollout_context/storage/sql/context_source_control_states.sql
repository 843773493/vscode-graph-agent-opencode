
    CREATE TABLE IF NOT EXISTS context_source_control_states (
        session_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        name TEXT NOT NULL,
        binding_revision TEXT,
        tracking_status TEXT NOT NULL CHECK(tracking_status IN ('tracked','untracked')),
        latest_visible_committed_revision TEXT,
        latest_revision TEXT,
        state_revision INTEGER NOT NULL CHECK(state_revision >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(session_id, thread_id, source_id),
        CHECK (binding_revision IS NULL OR length(binding_revision) > 0),
        CHECK (latest_visible_committed_revision IS NULL OR length(latest_visible_committed_revision) > 0),
        CHECK (latest_revision IS NULL OR length(latest_revision) > 0),
        CHECK (latest_revision IS NOT NULL OR latest_visible_committed_revision IS NULL)
    );
