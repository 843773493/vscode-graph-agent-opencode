-- 分页按消息定位工具调用，避免每次查询扫描全部调用并临时建索引。
CREATE INDEX IF NOT EXISTS tool_calls_message_index ON tool_calls(assistant_message_sequence, call_index);
-- 工具身份已由主键索引覆盖，移除重复索引，避免每次写入维护两份。
DROP INDEX IF EXISTS tool_calls_id_index;
