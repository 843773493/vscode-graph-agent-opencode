ALTER TABLE execution_queue ADD COLUMN queue_position INTEGER NOT NULL DEFAULT 0;
ALTER TABLE execution_queue ADD COLUMN priority INTEGER NOT NULL DEFAULT 0 CHECK(priority IN (0,1));
ALTER TABLE execution_queue ADD COLUMN dispatch_order INTEGER;
ALTER TABLE execution_queue ADD COLUMN notice_selection_json TEXT;
UPDATE execution_queue SET queue_position=admission_commit_id,dispatch_order=admission_commit_id;
UPDATE execution_queue SET notice_selection_json=(
    SELECT json_extract(c.metadata_json,'$.runtime_notice_selection')
    FROM executions e JOIN storage_commits c ON c.subject_id=e.turn_id AND c.commit_kind='acceptance' AND c.status='committed'
    WHERE e.execution_id=execution_queue.execution_id
);
CREATE INDEX execution_queue_scheduling ON execution_queue(checkpoint_ns,priority,queue_position) WHERE phase='queued';
CREATE INDEX execution_queue_dispatch_order ON execution_queue(dispatch_order);
CREATE TABLE user_questions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id TEXT NOT NULL UNIQUE,
    markdown TEXT NOT NULL,
    turn_id TEXT,
    model_call_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX user_questions_visible ON user_questions(deleted_at,sequence);
CREATE TABLE user_question_state (singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),revision INTEGER NOT NULL);
INSERT INTO user_question_state VALUES(1,0);
CREATE TRIGGER user_question_insert AFTER INSERT ON user_questions BEGIN
    UPDATE user_question_state SET revision=revision+1 WHERE singleton_id=1;
END;
CREATE TRIGGER user_question_update AFTER UPDATE ON user_questions BEGIN
    UPDATE user_question_state SET revision=revision+1 WHERE singleton_id=1;
END;
