CREATE TABLE IF NOT EXISTS collaboration_deliveries (
        ledger_revision INTEGER NOT NULL REFERENCES collaboration_fanouts(ledger_revision),
        recipient_thread_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','delivered','recipient_deleted','fence_closed')),
        error TEXT, PRIMARY KEY(ledger_revision, recipient_thread_id))
