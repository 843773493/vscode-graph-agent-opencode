-- 第14次迁移为 Python 的旧配置来源合并；本迁移为连接操作新增实际结构。
CREATE TABLE mcp_connection_operations (
    operation_id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('connect', 'disconnect')),
    configuration_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'waiting_user', 'verifying', 'succeeded', 'failed', 'expired', 'cancelled')),
    stage TEXT NOT NULL,
    created_at REAL NOT NULL,
    deadline REAL NOT NULL,
    updated_at REAL NOT NULL,
    error_code TEXT
);
CREATE UNIQUE INDEX mcp_connection_active_operation ON mcp_connection_operations(connection_id)
WHERE status IN ('pending', 'waiting_user', 'verifying');
CREATE INDEX mcp_connection_operation_history ON mcp_connection_operations(connection_id, created_at DESC);
-- 多个不同调用可以加入同一个授权操作；其幂等身份也必须在终态后继续解析原操作。
CREATE TABLE mcp_connection_operation_aliases (
    intent_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES mcp_connection_operations(operation_id) ON DELETE CASCADE
);
