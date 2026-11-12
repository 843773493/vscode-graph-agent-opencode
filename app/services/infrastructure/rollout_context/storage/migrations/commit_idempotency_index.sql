-- 每次普通追加都要检查幂等前像；非唯一索引保留最早 commit 的既有判定。
CREATE INDEX IF NOT EXISTS storage_commits_idempotency_index
    ON storage_commits(commit_kind, subject_id, idempotency_key);
