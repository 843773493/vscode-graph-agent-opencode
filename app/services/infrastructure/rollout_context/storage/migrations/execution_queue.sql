CREATE TABLE IF NOT EXISTS execution_queue_revision (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    revision INTEGER NOT NULL CHECK(revision>=0)
);
INSERT OR IGNORE INTO execution_queue_revision(singleton_id, revision) VALUES(1, 0);
CREATE TABLE IF NOT EXISTS execution_queue (
    checkpoint_ns TEXT NOT NULL,
    job_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    admission_commit_id INTEGER NOT NULL REFERENCES storage_commits(commit_id),
    input_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    delivery_policy TEXT NOT NULL CHECK(delivery_policy IN ('after_turn','after_tool_result','after_interrupt')),
    phase TEXT NOT NULL CHECK(phase IN ('queued','claimed','started','terminal')),
    claim_owner TEXT,
    claim_generation INTEGER NOT NULL DEFAULT 0 CHECK(claim_generation>=0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(checkpoint_ns, job_id),
    UNIQUE(checkpoint_ns, admission_commit_id),
    UNIQUE(checkpoint_ns, execution_id),
    CHECK((phase='queued' AND claim_owner IS NULL AND claim_generation=0)
       OR (phase IN ('claimed','started') AND claim_owner IS NOT NULL AND claim_generation>0)
       OR phase='terminal')
);
CREATE UNIQUE INDEX IF NOT EXISTS execution_queue_single_active
    ON execution_queue(checkpoint_ns) WHERE phase IN ('claimed','started');
CREATE INDEX IF NOT EXISTS execution_queue_runnable
    ON execution_queue(checkpoint_ns, admission_commit_id) WHERE phase<>'terminal';
CREATE TRIGGER IF NOT EXISTS execution_queue_terminal
AFTER UPDATE OF outcome ON executions
WHEN NEW.outcome<>'unknown'
BEGIN
    UPDATE execution_queue SET phase='terminal', updated_at=NEW.updated_at
        WHERE execution_id=NEW.execution_id AND phase<>'terminal';
END;
CREATE TRIGGER IF NOT EXISTS execution_queue_insert_revision AFTER INSERT ON execution_queue
BEGIN
    UPDATE execution_queue_revision SET revision=revision+1 WHERE singleton_id=1;
END;
CREATE TRIGGER IF NOT EXISTS execution_queue_update_revision AFTER UPDATE ON execution_queue
BEGIN
    UPDATE execution_queue_revision SET revision=revision+1 WHERE singleton_id=1;
END;
