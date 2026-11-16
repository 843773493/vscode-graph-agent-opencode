-- Thread 归属仅作正文派生索引，不保存第二份事件或可变 Job 事实。
ALTER TABLE trace_events ADD COLUMN thread_id TEXT;
CREATE INDEX trace_job_events ON trace_events(job_id, start);
DROP TRIGGER trace_update_revision;
CREATE TRIGGER trace_update_revision AFTER UPDATE OF raw ON trace_events BEGIN
    UPDATE trace_revision SET revision = revision + 1 WHERE id = 1;
END;
UPDATE trace_events SET thread_id = CASE
    WHEN event_type IN ('goal_updated', 'goal_cleared') THEN owner_thread
    ELSE (SELECT owner.owner_thread FROM trace_events AS owner INDEXED BY trace_job_owner
        WHERE owner.job_id = trace_events.job_id
          AND owner.event_type IN ('job_created', 'job_started', 'message_created')
        ORDER BY owner.start LIMIT 1)
END;
CREATE INDEX trace_thread_events ON trace_events(thread_id, start, end);
CREATE INDEX trace_unowned_job_events ON trace_events(job_id) WHERE thread_id IS NULL;

CREATE TRIGGER trace_insert_thread AFTER INSERT ON trace_events BEGIN
    UPDATE trace_events SET thread_id = CASE
        WHEN NEW.event_type IN ('goal_updated', 'goal_cleared') THEN NEW.owner_thread
        ELSE (SELECT owner_thread FROM trace_events INDEXED BY trace_job_owner WHERE job_id = NEW.job_id
            AND event_type IN ('job_created', 'job_started', 'message_created')
            ORDER BY start LIMIT 1)
    END WHERE start = NEW.start;
    -- 低层允许先写事件再写 owner；owner 到达时只补齐尚无归属的索引。
    UPDATE trace_events SET thread_id = (SELECT thread_id FROM trace_events WHERE start = NEW.start)
        WHERE NEW.event_type IN ('job_created', 'job_started', 'message_created')
          AND job_id = NEW.job_id AND thread_id IS NULL
          AND event_type NOT IN ('goal_updated', 'goal_cleared');
END;

-- 离线正文改写也必须同步索引；普通追加不会执行此历史更新。
CREATE TRIGGER trace_update_thread AFTER UPDATE OF raw ON trace_events BEGIN
    UPDATE trace_events SET thread_id = CASE
        WHEN event_type IN ('goal_updated', 'goal_cleared') THEN owner_thread
        ELSE (SELECT owner.owner_thread FROM trace_events AS owner INDEXED BY trace_job_owner
            WHERE owner.job_id = trace_events.job_id
              AND owner.event_type IN ('job_created', 'job_started', 'message_created')
            ORDER BY owner.start LIMIT 1)
    END WHERE job_id IN (OLD.job_id, NEW.job_id);
END;
