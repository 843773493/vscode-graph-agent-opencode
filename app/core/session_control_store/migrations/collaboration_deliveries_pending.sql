CREATE INDEX IF NOT EXISTS collaboration_deliveries_pending
        ON collaboration_deliveries(recipient_thread_id, ledger_revision) WHERE state='pending'
