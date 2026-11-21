CREATE TABLE execution_queue_pause (
    checkpoint_ns TEXT PRIMARY KEY,
    pause_id TEXT NOT NULL UNIQUE CHECK(length(pause_id)>0),
    execution_id TEXT REFERENCES executions(execution_id)
);
