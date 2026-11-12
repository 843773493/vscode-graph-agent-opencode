
CREATE INDEX IF NOT EXISTS idx_background_mailbox_owner
ON background_mailbox(thread_id,agent_id,sequence)
