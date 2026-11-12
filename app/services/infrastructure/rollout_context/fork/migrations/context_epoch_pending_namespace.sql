CREATE UNIQUE INDEX context_epoch_pending_namespace ON context_epoch_transitions(checkpoint_ns,target_view_id) WHERE status='pending' AND reason IN ('rewind','compaction')
