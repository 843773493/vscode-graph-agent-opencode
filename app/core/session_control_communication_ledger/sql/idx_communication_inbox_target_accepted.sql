CREATE INDEX IF NOT EXISTS idx_communication_inbox_target_accepted ON communication_inbox(state) WHERE state = 'target_accepted'
