
    CREATE INDEX IF NOT EXISTS context_source_control_states_owner_index
        ON context_source_control_states(session_id, thread_id, tracking_status);
