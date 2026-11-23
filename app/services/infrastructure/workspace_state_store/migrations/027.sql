-- 原连接操作保存安全原因；旧行 NULL 表示未记录，不推测历史原因。
ALTER TABLE mcp_connection_operations ADD COLUMN error_message TEXT;
