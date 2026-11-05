-- 输入消费证据保留同一 assembly 的全部重试，按请求定位后再选最早派发 attempt。
CREATE INDEX IF NOT EXISTS model_calls_assembly_index
ON model_calls(assembly_id, session_id, thread_id, execution_id, attempt);
