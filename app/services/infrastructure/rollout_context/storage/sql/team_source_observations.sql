
    CREATE TABLE IF NOT EXISTS team_source_observations (
        session_id TEXT NOT NULL, thread_id TEXT NOT NULL,
        ledger_revision INTEGER NOT NULL CHECK(ledger_revision >= 1),
        event_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
        PRIMARY KEY(session_id, thread_id, ledger_revision)
    );
