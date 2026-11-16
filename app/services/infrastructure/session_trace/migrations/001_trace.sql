CREATE TABLE trace_events (
    start INTEGER PRIMARY KEY CHECK (start >= 0),
    raw TEXT NOT NULL,
    end INTEGER GENERATED ALWAYS AS (start + length(CAST(raw AS BLOB))) STORED UNIQUE,
    event_id TEXT GENERATED ALWAYS AS (json_extract(raw, '$.event_id')) VIRTUAL,
    job_id TEXT GENERATED ALWAYS AS (json_extract(raw, '$.job_id')) VIRTUAL,
    event_type TEXT GENERATED ALWAYS AS (json_extract(raw, '$.type')) VIRTUAL,
    owner_session TEXT GENERATED ALWAYS AS (json_extract(raw, '$.payload.session_id')) VIRTUAL,
    owner_thread TEXT GENERATED ALWAYS AS (json_extract(raw, '$.payload.thread_id')) VIRTUAL,
    is_message INTEGER GENERATED ALWAYS AS (event_type IN (
        'job_created', 'job_started', 'job_completed', 'job_cancelled', 'job_failed',
        'message_created', 'status_change', 'text_start', 'text_end', 'tool_call_start',
        'tool_call_end', 'error', 'session_interrupted'
    )) VIRTUAL
);
CREATE INDEX trace_event_identity ON trace_events(event_id);
CREATE INDEX trace_job_owner ON trace_events(job_id, owner_session, owner_thread)
    WHERE event_type IN ('job_created', 'job_started', 'message_created');
CREATE INDEX trace_messages ON trace_events(start) WHERE is_message=1;
-- 搜索续页绑定整个已提交快照，追加或改写后必须明确失效。
CREATE TABLE trace_revision (id INTEGER PRIMARY KEY CHECK (id = 1), identity TEXT NOT NULL, revision INTEGER NOT NULL);
INSERT INTO trace_revision VALUES (1, lower(hex(randomblob(16))), 0);
CREATE TRIGGER trace_insert_revision AFTER INSERT ON trace_events BEGIN
    UPDATE trace_revision SET revision = revision + 1 WHERE id = 1;
END;
CREATE TRIGGER trace_update_revision AFTER UPDATE ON trace_events BEGIN
    UPDATE trace_revision SET revision = revision + 1 WHERE id = 1;
END;
CREATE TRIGGER trace_delete_revision AFTER DELETE ON trace_events BEGIN
    UPDATE trace_revision SET revision = revision + 1 WHERE id = 1;
END;
