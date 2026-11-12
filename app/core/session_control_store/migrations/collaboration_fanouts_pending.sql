CREATE INDEX IF NOT EXISTS collaboration_fanouts_pending
        ON collaboration_fanouts(state, ledger_revision) WHERE state='pending'
