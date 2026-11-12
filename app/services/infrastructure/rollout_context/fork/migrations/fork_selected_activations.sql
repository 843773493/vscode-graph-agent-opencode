CREATE TEMP TABLE fork_selected_activations(session_id TEXT,thread_id TEXT,activation_snapshot_id TEXT,visited INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(session_id,thread_id,activation_snapshot_id))
