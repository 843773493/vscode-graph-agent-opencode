-- 重试只读 Job 生命周期；不为筛选虚拟 event_type 读取同 Job 的全部输出正文。
CREATE INDEX trace_message_jobs ON trace_events(json_extract(raw, '$.payload.message_id'), job_id)
    WHERE event_type='job_created';
CREATE INDEX trace_job_lifecycle ON trace_events(job_id, start)
    WHERE event_type IN ('job_created', 'job_completed', 'job_cancelled',
                        'job_failed', 'session_interrupted');
