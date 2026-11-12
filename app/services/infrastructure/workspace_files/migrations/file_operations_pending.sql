CREATE INDEX file_operations_pending ON operations(queue_seq) WHERE state IN ('queued','running')
