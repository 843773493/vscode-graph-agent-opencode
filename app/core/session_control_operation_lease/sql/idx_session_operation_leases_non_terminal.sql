CREATE INDEX IF NOT EXISTS idx_session_operation_leases_non_terminal ON session_operation_leases(state) WHERE state IN ('active', 'settling')
