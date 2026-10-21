CREATE TABLE IF NOT EXISTS collaboration_fanouts (
        ledger_revision INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
        event_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
        lease_id TEXT NOT NULL UNIQUE REFERENCES session_operation_leases(lease_id),
        wakeup_json TEXT, wakeup_receipt_json TEXT,
        state TEXT NOT NULL CHECK(state IN ('pending','completed','cancelled')),
        created_at TEXT NOT NULL)
